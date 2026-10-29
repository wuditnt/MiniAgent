"""
图片输入（视觉）层测试。不需要 API Key，不花任何费用。

覆盖四处容易出错的地方：
  1. 格式嗅探     —— 按魔数识别，不信任扩展名
  2. 附件通道     —— attach/取走即清空，绝不串场
  3. 沙箱与拒绝   —— 越界 / 不存在 / 非图片 / 超大 全部安全拒绝
  4. 循环集成     —— view_image 的附件在回填工具结果后作为 user 消息进对话
"""

from __future__ import annotations

import os

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区
from bootstrap import PACKAGE_ROOT as HERE

from core.images import attach_image, take_images  # noqa: E402
from plugins.vision_tools import MAX_IMAGE_BYTES, _sniff  # noqa: E402

# 标准 1x1 透明 PNG（仅用于魔数与大小校验，不需要真的解码）
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
PROBE = os.path.join(HERE, "_vision_probe.png")


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


# ============================================================ 1. 格式嗅探

def test_sniff():
    assert _sniff(PNG_1X1) == "image/png"
    assert _sniff(b"\xff\xd8\xff\xe0junk") == "image/jpeg"
    assert _sniff(b"GIF89a...") == "image/gif"
    assert _sniff(b"RIFF1234WEBPVP8 ") == "image/webp"
    assert _sniff(b"just plain text") is None
    print("✓ 嗅探：png/jpeg/gif/webp 按魔数识别，文本不误判")


# ============================================================ 2. 附件通道

def test_attach_then_take_drains():
    take_images()   # 先清场
    attach_image("data:image/png;base64,AAAA", note="a.png")
    attach_image("data:image/jpeg;base64,BBBB")   # 无 note
    parts = take_images()
    assert parts[0]["type"] == "text" and "a.png" in parts[0]["text"]
    urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
    assert urls == ["data:image/png;base64,AAAA", "data:image/jpeg;base64,BBBB"]
    assert take_images() == [], "取走即清空，不能串到下一轮"
    print("✓ 附件通道：attach → take 组装分片，取走即清空")


# ============================================================ 3. 工具层

def test_view_image_attaches():
    import tools

    _write(PROBE, PNG_1X1)
    try:
        out = tools.call("view_image", {"path": "_vision_probe.png"})
        assert out.startswith("[image attached]"), out
        parts = take_images()
        assert any(
            p["type"] == "image_url"
            and p["image_url"]["url"].startswith("data:image/png;base64,")
            for p in parts
        ), parts
    finally:
        os.remove(PROBE)
    print("✓ view_image：读取工作区图片 → 附加 data URL，返回文字摘要")


def test_view_image_rejects():
    import tools

    bad = os.path.join(HERE, "_vision_bad.txt")
    _write(bad, b"this is not an image at all")
    try:
        out = tools.call("view_image", {"path": "_vision_bad.txt"})
        assert "不是可识别的图片格式" in out, out

        out = tools.call("view_image", {"path": "_no_such.png"})
        assert "文件不存在" in out, out

        out = tools.call("view_image", {"path": "../../../Windows/win.ini"})
        assert "越界" in out, out

        big = os.path.join(HERE, "_vision_big.png")
        _write(big, PNG_1X1[:8] + b"\x00" * (MAX_IMAGE_BYTES + 1))
        try:
            out = tools.call("view_image", {"path": "_vision_big.png"})
            assert "图片过大" in out, out
        finally:
            os.remove(big)

        assert take_images() == [], "被拒绝的调用不能留下附件"
    finally:
        os.remove(bad)
    print("✓ 拒绝：非图片 / 不存在 / 越界 / 超大 全部安全拒绝且无附件残留")


# ============================================================ 4. 循环集成

def _scripted_client(msgs):
    """与 test_loop.py 相同的假客户端：按脚本依次返回响应。"""

    class _Fn:
        def __init__(self, name, arguments):
            self.name, self.arguments = name, arguments

    class _TC:
        def __init__(self, i, name, arguments):
            self.id, self.type = f"call_{i}", "function"
            self.function = _Fn(name, arguments)

    class _Msg:
        def __init__(self, content=None, tool_calls=None):
            self.content, self.tool_calls = content, tool_calls

    class _Resp:
        def __init__(self, msg):
            self.choices = [type("C", (), {"message": msg})]

    class _Scripted:
        def __init__(self, msgs):
            self._msgs = list(msgs)

        def create(self, **kwargs):
            return _Resp(self._msgs.pop(0))

    class _Stub:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": _Scripted(msgs)})()

    return _Stub()


def test_agent_loop_appends_image_message():
    import agent as agent_mod
    import tools  # noqa: F401  —— 注册 view_image

    take_images()   # 清场
    _write(PROBE, PNG_1X1)
    client = _scripted_client(
        [
            type("M", (), {
                "content": None,
                "tool_calls": [type("TC", (), {
                    "id": "call_1", "type": "function",
                    "function": type("F", (), {"name": "view_image",
                                               "arguments": '{"path": "_vision_probe.png"}'})(),
                })()],
            })(),
            type("M", (), {"content": "图里是一个像素。", "tool_calls": None})(),
        ]
    )
    try:
        a = agent_mod.Agent(verbose=False, client=client)
        out = a.run("看看这张图")
        assert out == "图里是一个像素。", out

        roles = [m["role"] for m in a.messages]
        assert roles == ["system", "user", "assistant", "tool", "user", "assistant"], roles
        img_msg = a.messages[4]
        assert img_msg["role"] == "user"
        assert any(
            p["type"] == "image_url"
            and p["image_url"]["url"].startswith("data:image/png;base64,")
            for p in img_msg["content"]
        ), img_msg
        assert take_images() == [], "附件应当已被主循环取走"
    finally:
        os.remove(PROBE)
    print("✓ 循环集成：工具结果回填后，图片作为 user 消息追加，模型下一轮可见")


if __name__ == "__main__":
    print("项目根:", HERE)
    print("---")
    test_sniff()
    test_attach_then_take_drains()
    test_view_image_attaches()
    test_view_image_rejects()
    test_agent_loop_appends_image_message()
    print("---")
    print("全部通过 ✅")