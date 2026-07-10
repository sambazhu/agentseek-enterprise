from __future__ import annotations

from agentseek_files.content_analysis import analyze_content, group_counts, unique_people


def test_mineru_table_skips_merged_titles_before_real_header() -> None:
    text = (
        '<table><tr><td colspan="6">2026 年春季午餐退费明细公示表</td></tr>'
        '<tr><td colspan="6">学校：示例小学；期间：三月至六月</td></tr>'
        '<tr><td colspan="6">备注：以下金额以元为单位</td></tr>'
        "<tr><td>序号</td><td>姓名</td><td>班级</td><td>退费餐数</td><td>退费金额</td><td>明细</td></tr>"
        "<tr><td>1</td><td>张三</td><td>一一班</td><td>2</td><td>20</td><td>3月</td></tr>"
        "<tr><td>2</td><td>李四</td><td>一一班</td><td>3</td><td>30</td><td>4月</td></tr>"
        "<tr><td>3</td><td>张三</td><td>一二班</td><td>1</td><td>10</td><td>5月</td></tr>"
        "</table>"
    )

    analysis = analyze_content(text)

    assert analysis.headers == ("序号", "姓名", "班级", "退费餐数", "退费金额", "明细")
    assert analysis.data_rows == 3
    assert unique_people(analysis) == 2
    assert group_counts(analysis, "每班有多少个人退餐？") == (
        "班级",
        [("一一班", 2), ("一二班", 1)],
    )


def test_simple_table_still_uses_first_row_as_header() -> None:
    analysis = analyze_content(
        "<table><tr><td>商品</td><td>库存</td></tr>"
        "<tr><td>铅笔</td><td>12</td></tr>"
        "<tr><td>钢笔</td><td>8</td></tr></table>"
    )

    assert analysis.headers == ("商品", "库存")
    assert analysis.data_rows == 2
