# pip install polars fastexcel
"""
大数据表格分析工具组（基于 polars）。

和邻居工具的分工：
  · data_tools       管「小文件的读一眼」（read_csv / read_json，纯 stdlib，够用就行）
  · database_tools  管「sqlite 与 CSV 的 SQL 查询」（内存 sqlite，小数据）
  · dataframe_tools 管「大文件的真分析」（列式内存、惰性扫描、SQL、落 Parquet）

设计取舍：
  · polars 懒加载：没装只让这几个工具返回提示，不影响工具整体加载。
  · 只读走 read_path、写走 safe_path，与其他工具一样受路径沙箱约束。
  · CSV 默认按 UTF-8 读；读不动（如 Excel 导出的 GBK）再按探测到的编码解码后重试。
"""

from __future__ import annotations

import io
import os

from core import (
    clip,
    decode_bytes,
    read_bytes,
    read_path,
    safe_path,
    tool,
)

TOOL_INFO = {
    "name": "表格分析",
    "version": "1.0",
    "author": "mini-agent",
    "desc": "用 polars 做大表格分析：概览 / SQL / 转 Parquet（依赖 polars；读 Excel 需 fastexcel）",
}

# 一次最多渲染多少行到 Markdown
MAX_SHOW_ROWS = 50
# 单个单元格最多渲染多少字符
MAX_CELL = 80

_EXCEL_EXTS = (".xlsx", ".xlsm", ".xls", ".xlsb", ".ods")


def _need(lib: str, pip_name: str = "") -> str:
    return (
        f"[error] 需要第三方库 {lib}，当前环境未安装。\n"
        f"  安装：python -m pip install {pip_name or lib}\n"
        f"  （装到本项目虚拟环境，勿用系统 Python）"
    )


def _read_csv(pl, ap: str, sep: str):
    """先按 UTF-8 直接读（最快）；失败再探测编码解码后重喂给 polars。"""
    try:
        return pl.read_csv(ap, separator=sep, try_parse_dates=True, infer_schema_length=10000)
    except Exception as e_first:
        try:
            text, _ = decode_bytes(read_bytes(ap))
            return pl.read_csv(
                io.BytesIO(text.encode("utf-8")),
                separator=sep,
                try_parse_dates=True,
                infer_schema_length=10000,
            )
        except Exception:
            raise e_first


def _load(ap: str, sheet: str = ""):
    """按扩展名把文件读成 polars DataFrame；不支持的格式返回错误字符串。"""
    import polars as pl

    ext = os.path.splitext(ap)[1].lower()
    if ext in (".csv", ".tsv", ".txt"):
        return _read_csv(pl, ap, "\t" if ext == ".tsv" else ",")
    if ext == ".json":
        return pl.read_json(ap)
    if ext in (".jsonl", ".ndjson"):
        return pl.read_ndjson(ap)
    if ext in (".parquet", ".pq"):
        return pl.read_parquet(ap)
    if ext in _EXCEL_EXTS:
        # 注意：polars 的 Excel 读取走 fastexcel，sheet 只认表名（用 None 表示第一张），
        # 传索引 0 会报「no matching sheets found」。
        return pl.read_excel(ap, sheet_name=sheet or None)
    return f"[error] 不支持的格式 {ext or '(无扩展名)'}；支持 csv/tsv/json/jsonl/parquet/xlsx/xls/ods"


def _cell(v) -> str:
    if v is None:
        return ""
    s = str(v)
    return s[:MAX_CELL] + "…" if len(s) > MAX_CELL else s


def _render(df) -> str:
    """DataFrame → Markdown 表格（列名做表头）。"""
    cols = list(df.columns)
    if not cols:
        return "（无列）"
    head = "| " + " | ".join(cols) + " |"
    sep = "|" + "|".join(["---"] * len(cols)) + "|"
    body = ["| " + " | ".join(_cell(v) for v in r) + " |" for r in df.iter_rows()]
    return "\n".join([head, sep] + body)


@tool(
    "df_summary",
    "用 polars 快速概览一个大表格文件：行列数、每列类型与空值数、前几行内容、数值列统计。"
    "支持 .csv/.tsv/.json/.jsonl/.parquet/.xlsx/.xls/.ods。依赖第三方库 polars。",
    params={
        "path": {"type": "string", "description": "数据文件路径"},
        "sheet": {"type": "string", "description": "Excel 工作表名（仅 Excel 用）", "default": ""},
        "rows": {"type": "integer", "description": "预览行数", "default": 5},
    },
    required=["path"],
)
def df_summary(path: str, sheet: str = "", rows: int = 5) -> str:
    try:
        import polars as pl  # noqa: F401
    except ImportError:
        return _need("polars")

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"

    try:
        df = _load(ap, sheet)
    except Exception as e:  # noqa: BLE001
        return f"[error] 读取 {ap} 失败（{type(e).__name__}: {e}）"
    if isinstance(df, str):
        return df

    cols = list(df.columns)
    types = ", ".join(f"{c}:{str(t).lower()}" for c, t in zip(cols, df.dtypes))
    nulls = df.null_count()
    null_line = ", ".join(f"{c}={n}" for c, n in zip(cols, nulls.row(0))) if cols else "（无列）"

    n = max(1, rows)
    out = [
        f"[{ap}] {df.height} 行 × {df.width} 列",
        f"  类型: {types}",
        f"  空值: {null_line}",
        f"  前 {min(n, df.height)} 行:",
        _render(df.head(n)),
    ]

    # 数值列的分布统计（非数值列 polars 只给 count/null_count/min/max，略去以省上下文）
    numeric = df.select([c for c, t in zip(cols, df.dtypes) if t.is_numeric()])
    if numeric.width:
        out += ["  数值列统计:", _render(numeric.describe())]

    return clip("\n".join(out))


@tool(
    "df_sql",
    "用 polars 的 SQL 引擎查询大表格文件（表名固定为 t）。"
    "适合 group by / join / 聚合这类分析；小文件用 csv_sql 也行。"
    "支持 .csv/.tsv/.json/.jsonl/.parquet/.xlsx/.xls/.ods。依赖第三方库 polars。",
    params={
        "path": {"type": "string", "description": "数据文件路径"},
        "sql": {"type": "string", "description": "SQL 语句，表名用 t（如 SELECT a, sum(b) FROM t GROUP BY a）"},
        "sheet": {"type": "string", "description": "Excel 工作表名（仅 Excel 用）", "default": ""},
        "limit": {"type": "integer", "description": "最多显示多少行结果", "default": MAX_SHOW_ROWS},
    },
    required=["path", "sql"],
)
def df_sql(path: str, sql: str, sheet: str = "", limit: int = MAX_SHOW_ROWS) -> str:
    try:
        import polars as pl
    except ImportError:
        return _need("polars")

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 文件不存在：{ap}"

    try:
        df = _load(ap, sheet)
    except Exception as e:  # noqa: BLE001
        return f"[error] 读取 {ap} 失败（{type(e).__name__}: {e}）"
    if isinstance(df, str):
        return df

    try:
        result = pl.SQLContext().register("t", df).execute(sql).collect()
    except Exception as e:  # noqa: BLE001
        return f"[error] SQL 执行失败（{type(e).__name__}: {e}）\n可用列：{', '.join(df.columns)}"

    n = max(1, limit)
    head = f"[{ap}] 结果 {result.height} 行 × {result.width} 列"
    if result.height > n:
        head += f"（只显示前 {n} 行）"
    return clip(f"{head}\n{_render(result.head(n))}")


@tool(
    "df_to_parquet",
    "把 CSV/JSON/Excel 转成 Parquet（列式、带压缩），大文件后续读取会快很多。"
    "依赖第三方库 polars。",
    params={
        "path": {"type": "string", "description": "源文件路径"},
        "out": {"type": "string", "description": "输出的 parquet 路径"},
        "sheet": {"type": "string", "description": "Excel 工作表名（仅 Excel 用）", "default": ""},
        "compression": {"type": "string", "enum": ["zstd", "lz4", "snappy", "uncompressed"],
                        "description": "压缩算法", "default": "zstd"},
    },
    required=["path", "out"],
)
def df_to_parquet(path: str, out: str, sheet: str = "", compression: str = "zstd") -> str:
    try:
        import polars as pl  # noqa: F401
    except ImportError:
        return _need("polars")

    ap = read_path(path)
    if not os.path.isfile(ap):
        return f"[error] 源文件不存在：{ap}"

    op = safe_path(out)
    os.makedirs(os.path.dirname(op) or ".", exist_ok=True)

    try:
        df = _load(ap, sheet)
    except Exception as e:  # noqa: BLE001
        return f"[error] 读取 {ap} 失败（{type(e).__name__}: {e}）"
    if isinstance(df, str):
        return df

    try:
        df.write_parquet(op, compression=compression)
    except Exception as e:  # noqa: BLE001
        return f"[error] 写 Parquet 失败（{type(e).__name__}: {e}）"

    return (
        f"[ok] {df.height} 行 × {df.width} 列已写入 {op}"
        f"（{os.path.getsize(op)} 字节，compression={compression}）"
    )
