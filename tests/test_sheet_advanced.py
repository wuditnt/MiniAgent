"""
第二批 2B 测试：Excel 合并 / 拆分 / CSV 互转 / 格式化 / 批量公式。

不需要 API Key、不花钱。临时文件建在项目根下，保证沙箱判定与真实运行一致。

覆盖：
  1. 6 个新工具已注册，sheet_tools 不依赖兄弟模块
  2. xlsx_merge：按工作表合并、按数据行拼接（保留/跳过表头、来源列）
  3. xlsx_split：按工作表拆分、按行数拆分（每片带表头）
  4. xlsx_to_csv / csv_to_xlsx：中文与数字往返、前导零保持文本
  5. xlsx_format：表头加粗/底色/冻结/筛选/自适应列宽
  6. xlsx_formula：公式按相对引用自动平移，非法公式被拒绝
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager

import bootstrap  # noqa: F401  —— 必须最先导入：铺 sys.path + 定工作区
from bootstrap import PACKAGE_ROOT

from core.registry import REGISTRY  # noqa: E402

from builtin_tools import sheet_tools  # noqa: E402
import tools  # noqa: E402,F401  —— 导入即加载全部内置工具


@contextmanager
def temp_dir():
    path = tempfile.mkdtemp(prefix="sheet-adv-test-", dir=PACKAGE_ROOT)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _skip_if_missing(*modules: str) -> bool:
    try:
        for name in modules:
            __import__(name)
    except ImportError as e:
        print(f"SKIP：缺少依赖 {e.name}")
        return True
    return False


def _book(path: str, sheets: dict[str, list[list]]) -> None:
    """按 {表名: 行} 生成一个测试用 xlsx。"""
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title=title)
        for r in rows:
            ws.append(r)
    wb.save(path)


def test_tool_registration_and_independence():
    names = {t.name for t in REGISTRY.all()}
    expected = {
        "xlsx_merge", "xlsx_split", "xlsx_to_csv",
        "csv_to_xlsx", "xlsx_format", "xlsx_formula",
    }
    missing = expected - names
    assert not missing, f"缺少工具：{missing}"

    src = open(sheet_tools.__file__, encoding="utf-8").read()
    assert "from builtin_tools" not in src, "sheet_tools 不应依赖兄弟模块"
    assert "from ." not in src, "sheet_tools 不应使用相对导入"
    print("✓ 注册与独立性：6 个新工具已注册，sheet_tools 无兄弟依赖")


def test_xlsx_merge():
    if _skip_if_missing("openpyxl"):
        return
    with temp_dir() as d:
        a = os.path.join(d, "a.xlsx")
        b = os.path.join(d, "b.xlsx")
        _book(a, {
            "表一": [["姓名", "年龄"], ["张三", 18], ["李四", 20]],
            "表二": [["城市"], ["北京"], ["上海"]],
        })
        _book(b, {"数据": [["姓名", "年龄"], ["王五", 22]]})

        # 按工作表合并：1 个文件 2 表 + 1 个文件 1 表 = 3 张表
        out = os.path.join(d, "by_sheets.xlsx")
        ret = sheet_tools.xlsx_merge([a, b], out, mode="sheets")
        assert ret.startswith("[ok]"), ret
        import openpyxl
        wb = openpyxl.load_workbook(out, read_only=True)
        assert len(wb.sheetnames) == 3, wb.sheetnames
        assert "a_表一" in wb.sheetnames and "b_数据" in wb.sheetnames, wb.sheetnames
        wb.close()

        # 按行拼接：保留首个表头、跳过后续表头、追加来源列
        out = os.path.join(d, "by_rows.xlsx")
        ret = sheet_tools.xlsx_merge([a, b], out, mode="rows", source_column="来源")
        assert ret.startswith("[ok]"), ret
        rows = sheet_tools._sheet_rows(out)
        assert len(rows) == 4, rows  # 表头 + 2 行 a + 1 行 b
        assert rows[0][:3] == ["来源", "姓名", "年龄"], rows[0]
        assert rows[1][0] == "a.xlsx", rows[1]
        assert sum("a.xlsx" in str(c) for c in rows) == 2, rows
        assert any("b.xlsx" in str(c) for c in rows[-1]), rows[-1]
    print("✓ xlsx_merge：按表合并 3 张 / 按行拼接 4 行，表头与来源列正确")


def test_xlsx_split():
    if _skip_if_missing("openpyxl"):
        return
    with temp_dir() as d:
        src = os.path.join(d, "src.xlsx")
        _book(src, {
            "一月": [["姓名", "金额"], ["甲", 10], ["乙", 20]],
            "二月": [["姓名", "金额"], ["丙", 30], ["丁", 40], ["戊", 50]],
        })

        out_dir = os.path.join(d, "by_sheet")
        ret = sheet_tools.xlsx_split(src, out_dir, mode="sheets")
        assert ret.startswith("[ok]"), ret
        assert len(os.listdir(out_dir)) == 2, os.listdir(out_dir)

        out_dir = os.path.join(d, "by_rows")
        ret = sheet_tools.xlsx_split(src, out_dir, mode="rows", rows_per_file=1,
                                     sheet="二月")
        assert ret.startswith("[ok]"), ret
        files = sorted(os.listdir(out_dir))
        assert len(files) == 3, files  # 三月数据行按 1 行/份
        for f in files:
            rows = sheet_tools._sheet_rows(os.path.join(out_dir, f))
            assert rows[0] == ["姓名", "金额"], rows  # 每片都保留表头
            assert len(rows) == 2, rows
    print("✓ xlsx_split：按表 2 个文件 / 按行 3 个分片，分片均带表头")


def test_csv_roundtrip():
    if _skip_if_missing("openpyxl"):
        return
    with temp_dir() as d:
        src = os.path.join(d, "data.xlsx")
        _book(src, {
            "人员": [["编号", "姓名", "分数"], ["001", "张三", 90], ["002", "李四", 85]],
            "备注": [["说明"], ["中文导出"]],
        })

        out_dir = os.path.join(d, "csv")
        ret = sheet_tools.xlsx_to_csv(src, out_dir)
        assert ret.startswith("[ok]"), ret
        files = sorted(os.listdir(out_dir))
        assert len(files) == 2, files

        person_csv = next(os.path.join(out_dir, f) for f in files
                          if "人员" in f)
        with open(person_csv, encoding="utf-8-sig") as f:
            text = f.read()
        assert "张三" in text, text

        out = os.path.join(d, "back.xlsx")
        ret = sheet_tools.csv_to_xlsx([os.path.join(out_dir, f) for f in files], out)
        assert ret.startswith("[ok]"), ret
        rows = sheet_tools._sheet_rows(out, sheet_tools._sheet_names(out)[0])
        assert rows[0] == ["编号", "姓名", "分数"], rows
        assert rows[1] == ["001", "张三", 90], rows  # 前导零保持文本，分数转数值
        assert isinstance(rows[1][2], (int, float)), rows
    print("✓ xlsx_to_csv / csv_to_xlsx：中文与数字往返正确，前导零未丢失")


def test_xlsx_format():
    if _skip_if_missing("openpyxl"):
        return
    with temp_dir() as d:
        src = os.path.join(d, "fmt.xlsx")
        _book(src, {"表": [["姓名", "备注"], ["张三", "很长的中文备注内容"], ["李四", "短"]]})

        ret = sheet_tools.xlsx_format(src)
        assert ret.startswith("[ok]"), ret

        import openpyxl
        wb = openpyxl.load_workbook(src)
        ws = wb["表"]
        assert ws["A1"].font.bold is True, ws["A1"].font.bold
        assert "DDEBF7" in str(ws["A1"].fill.fgColor.rgb).upper(), ws["A1"].fill.fgColor.rgb
        assert ws.freeze_panes == "A2", ws.freeze_panes
        assert ws.auto_filter.ref, ws.auto_filter.ref
        assert ws.column_dimensions["B"].width >= 8, ws.column_dimensions["B"].width
        wb.close()
    print("✓ xlsx_format：表头样式 / 冻结 / 筛选 / 自适应列宽全部生效")


def test_xlsx_formula():
    if _skip_if_missing("openpyxl"):
        return
    with temp_dir() as d:
        src = os.path.join(d, "calc.xlsx")
        _book(src, {"数据": [["A", "B"], [1, 2], [3, 4], [5, 6]]})

        ret = sheet_tools.xlsx_formula(src, "C2:C4", "=A2*B2")
        assert ret.startswith("[ok]"), ret

        import openpyxl
        wb = openpyxl.load_workbook(src)  # data_only=False：读公式文本
        ws = wb["数据"]
        assert ws["C2"].value == "=A2*B2", ws["C2"].value
        assert ws["C3"].value == "=A3*B3", ws["C3"].value
        assert ws["C4"].value == "=A4*B4", ws["C4"].value
        wb.close()

        ret = sheet_tools.xlsx_formula(src, "D2", "A2*B2")
        assert ret.startswith("[error]"), ret
    print("✓ xlsx_formula：公式按行平移，缺 = 被拒绝")


if __name__ == "__main__":
    print("工作区:", PACKAGE_ROOT)
    print("---")
    test_tool_registration_and_independence()
    test_xlsx_merge()
    test_xlsx_split()
    test_csv_roundtrip()
    test_xlsx_format()
    test_xlsx_formula()
    print("---")
    print("全部通过 ✅")
