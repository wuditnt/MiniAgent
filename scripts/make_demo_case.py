"""
生成一份「有标准答案」的沙箱测试数据，用来实测 Agent 的真实分析能力。

设计意图（对应本次会话的四条经验）：
  1. 编码陷阱  —— 两个内容相同、编码不同的 CSV（GBK vs UTF-8+BOM）
  2. 压缩陷阱  —— 二进制里藏着 zlib 流，直接 grep 搜不到，必须先解压
  3. 二进制陷阱 —— PNG 含 NUL 字节，文本方式读会失败
  4. 诱饵      —— 出现 1 次的长串不该被当成"重复 token"上报

运行：python scripts/make_demo_case.py
      （从项目根跑；脚本自己会定位 demo_case/，与 cwd 无关）
"""

from __future__ import annotations

import hashlib
import os
import struct
import zlib

# 本脚本住在 scripts/，而 demo_case/ 在项目根 —— 上溯一层。
# 刻意不用 cwd：这个脚本常被手工单独执行，cwd 未必是项目根。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(PROJECT_ROOT, "demo_case", "inbox")

CSV_TEXT = """姓名,部门,金额,日期
张三,研发部,1234.50,2026-10-01
李四,市场部,5678.00,2026-10-02
王五,财务部,9012.75,2026-10-03
赵六,运营部,3456.25,2026-10-04
孙七,技术部,7890.10,2026-10-05
"""

# packet.bin 解压后要出现的「重复 token」—— 这是标准答案
REPEAT_TOKENS = {
    "9F3A77B2C1D4E560": 13,   # 主追踪号
    "0B9144CE88FF": 5,        # 会话号
    "77C1D4E5609F3A77": 7,    # 批次号
}

# 只出现 1 次的长串（诱饵：不应被算进「重复」）
ONE_OFF = ["A1B2C3D4E5F60718", "5F6E7D8C9B0A1234", "DEADBEEFCAFEF00D"]


def build_packets() -> bytes:
    """构造一段文本，其中三个 token 按预设次数重复出现。"""
    lines: list[str] = []
    seq = 0
    # 按「最大重复数」铺满各行，让三个 token 交错出现
    for i in range(max(REPEAT_TOKENS.values())):
        for tok, n in REPEAT_TOKENS.items():
            if i < n:
                seq += 1
                lines.append(f"[2026-10-14T09:{i:02d}:{seq:02d}] trace={tok} src=10.0.{i}.{seq} ok")
    for j, tok in enumerate(ONE_OFF):
        seq += 1
        lines.append(f"[2026-10-14T10:{j:02d}:{seq:02d}] trace={tok} src=10.1.{j}.{seq} ok")
    lines.append("-- end of batch --")
    return ("\n".join(lines) + "\n").encode("utf-8")


def make_png(w: int = 16, h: int = 16) -> bytes:
    """纯标准库生成一个合法 PNG（避免依赖 Pillow）。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b""
    for y in range(h):
        raw += b"\x00"  # 每行的 filter 字节
        for x in range(w):
            raw += bytes([(x * 16) % 256, (y * 16) % 256, 160])
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8bit truecolor
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    os.makedirs(ROOT, exist_ok=True)

    # 1) 同内容、不同编码的两个 CSV
    gbk_path = os.path.join(ROOT, "sales_gbk.csv")
    utf_path = os.path.join(ROOT, "sales_utf8bom.csv")
    with open(gbk_path, "wb") as f:
        f.write(CSV_TEXT.encode("gbk"))
    with open(utf_path, "wb") as f:
        f.write(b"\xef\xbb\xbf" + CSV_TEXT.encode("utf-8"))

    # 2) 压缩二进制：明文 -> zlib
    payload = build_packets()
    packet_path = os.path.join(ROOT, "packet.bin")
    with open(packet_path, "wb") as f:
        f.write(zlib.compress(payload, 9))

    # 3) 二进制图片
    png_path = os.path.join(ROOT, "logo.png")
    with open(png_path, "wb") as f:
        f.write(make_png())

    # 4) 背景说明（含干扰：只提到 token 名，不含真实计数）
    readme = os.path.join(ROOT, "README.txt")
    with open(readme, "w", encoding="utf-8") as f:
        f.write(
            "本目录是运维导出的月度对账数据快照。\n"
            "sales_*.csv 为同一份表格的两种编码导出。\n"
            "packet.bin 为压缩后的请求日志分片，需解压后才能阅读。\n"
            "logo.png 为公司标识。\n"
        )

    # ---------------------------------------------------------- 标准答案
    gbk_b = open(gbk_path, "rb").read()
    utf_b = open(utf_path, "rb").read()
    pk = open(packet_path, "rb").read()

    print("=" * 66)
    print("沙箱数据已生成:", ROOT)
    print("=" * 66)
    for p in (gbk_path, utf_path, packet_path, png_path, readme):
        b = open(p, "rb").read()
        print(f"  {os.path.basename(p):22s} {len(b):7d} B  md5={hashlib.md5(b).hexdigest()[:12]}")
    print()

    print("【标准答案 1】编码对比")
    print(f"  sales_gbk.csv      编码=GBK        无BOM   首3字节={gbk_b[:3].hex(' ')}")
    print(f"  sales_utf8bom.csv  编码=UTF-8 BOM          首3字节={utf_b[:3].hex(' ')}  (EF BB BF)")
    same = gbk_b.decode("gbk") == utf_b.decode("utf-8-sig")
    n_cjk = sum(1 for ch in CSV_TEXT if ord(ch) > 127)
    print(f"  解码后文本内容是否完全相同: {same}")
    print(f"  体积差: {len(utf_b) - len(gbk_b)} 字节  "
          f"(BOM 3 + 非ASCII字符 {n_cjk} 个 × (UTF8-3 - GBK-2) = 3 + {n_cjk} = {3 + n_cjk})")
    print()

    print("【标准答案 2】packet.bin 解压后的重复 token")
    print(f"  压缩后 {len(pk)} B  ->  解压后 {len(payload)} B")
    print(f"  原始字节里能否直接搜到主 token: {b'9F3A77B2C1D4E560' in pk}   <-- 所以必须先解压")
    for tok, n in REPEAT_TOKENS.items():
        print(f"    {tok}  x{n}    (实际出现 {payload.decode().count(tok)})")
    print(f"  诱饵(只出现1次，不应计入重复): {', '.join(ONE_OFF)}")
    print()

    print("【标准答案 3】logo.png 魔数")
    print(f"  前8字节 = {open(png_path,'rb').read()[:8].hex(' ')}  "
          f"(89 50 4E 47 0D 0A 1A 0A = \\x89PNG)")
    print()


if __name__ == "__main__":
    main()
