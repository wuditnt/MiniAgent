"""
数据库查询工具组。

sqlite_query  直接对 .sqlite / .db 文件执行 SQL
csv_sql       把 CSV 加载到内存 sqlite，再用 SQL 查

都用 stdlib sqlite3，零外部依赖。
"""

from __future__ import annotations

import csv
import os
import sqlite3

from core import decode_bytes, read_bytes, safe_path, tool

PLUGIN_INFO = {"name": "数据库", "version": "1.0", "author": "mini-agent"}


def _format_table(cursor: sqlite3.Cursor, rows: list, limit: int) -> str:
    cols = [d[0] for d in cursor.description or []]
    if not cols:
        return f"[ok] 影响 {cursor.rowcount} 行"
    # 只展示前 limit 行
    shown = rows[:limit]
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = [
        "| " + " | ".join(str(v)[:100] for v in r) + " |"
        for r in shown
    ]
    tail = f"\n…（共 {len(rows)} 行，只显示前 {limit} 行）" if len(rows) > limit else ""
    return "\n".join([header, sep] + body) + tail


@tool(
    "sqlite_query",
    "对 SQLite 数据库文件执行 SQL（SELECT/INSERT/UPDATE/DELETE/CREATE 等）。SELECT 以表格返回。",
    params={
        "db": {"type": "string", "description": "SQLite 数据库文件路径（.sqlite/.db）"},
        "sql": {"type": "string", "description": "要执行的 SQL 语句"},
        "limit": {"type": "integer", "description": "SELECT 结果最多显示的行数", "default": 50},
    },
    required=["db", "sql"],
    dangerous=True,
)
def sqlite_query(db: str, sql: str, limit: int = 50) -> str:
    ap = safe_path(db)
    if not os.path.isfile(ap):
        return f"[error] 数据库文件不存在：{ap}"
    try:
        conn = sqlite3.connect(ap)
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        if sql.strip().lower().startswith(("select", "with", "pragma", "explain")):
            out = _format_table(cur, rows, limit)
        else:
            conn.commit()
            out = f"[ok] 执行成功，影响 {cur.rowcount} 行"
        conn.close()
        return out
    except sqlite3.Error as e:
        return f"[error] SQLite 错误：{e}"


@tool(
    "csv_sql",
    "把 CSV 文件加载到内存里的 SQLite，然后用 SQL 查询。表名默认为 csv。",
    params={
        "csv_path": {"type": "string", "description": "CSV 文件路径"},
        "sql": {"type": "string", "description": "SQL 查询，用 csv 作为表名（如 SELECT * FROM csv WHERE age > 30）"},
        "table": {"type": "string", "description": "内存表名", "default": "csv"},
        "limit": {"type": "integer", "description": "结果最多显示行数", "default": 50},
    },
    required=["csv_path", "sql"],
)
def csv_sql(csv_path: str, sql: str, table: str = "csv", limit: int = 50) -> str:
    ap = safe_path(csv_path)
    if not os.path.isfile(ap):
        return f"[error] CSV 文件不存在：{ap}"

    text, _ = decode_bytes(read_bytes(ap))
    # 探测分隔符
    try:
        sample = text[:4096]
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(text.splitlines(), dialect)
    headers = next(reader)
    # 列名清洗：sqlite 不接受含空格/特殊字符的列名，用引号包起来
    clean_cols = [f'"{h.strip()}"' for h in headers]

    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute(f'CREATE TABLE "{table}" ({", ".join(clean_cols)})')
    placeholders = ", ".join("?" for _ in headers)
    cur.executemany(
        f'INSERT INTO "{table}" ({", ".join(clean_cols)}) VALUES ({placeholders})',
        (row for row in reader if row),
    )
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        out = _format_table(cur, rows, limit)
    except sqlite3.Error as e:
        out = f"[error] SQL 错误：{e}\n可用列：{', '.join(h.strip() for h in headers)}"
    conn.close()
    return out
