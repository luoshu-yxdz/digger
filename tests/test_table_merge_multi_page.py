from bs4 import BeautifulSoup

from mineru.utils.enum_class import BlockType
from mineru.utils.table_merge import merge_table


def _table_block(rows, continuation=False, page_index=0):
    html = "<table><tbody>" + "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    ) + "</tbody></table>"
    body = {
        "type": BlockType.TABLE_BODY,
        "index": page_index,
        "bbox": [0, 100, 100, 900],
        "lines": [{"spans": [{"html": html}]}],
    }
    blocks = [body]
    if continuation:
        blocks.insert(0, {
            "type": BlockType.TABLE_CAPTION,
            "lines": [{"spans": [{"type": "text", "content": "续表"}]}],
        })
    return {
        "type": BlockType.TABLE,
        "bbox": [0, 100, 100, 900],
        "blocks": blocks,
    }


def _page(table):
    return {"para_blocks": [table]}


def test_merge_continues_across_three_pages():
    header = ["项目", "地区"]
    pages = [
        _page(_table_block([header, ["项目甲", "宜昌"]], page_index=0)),
        _page(_table_block([header, ["项目乙", "宜都"]], continuation=True, page_index=1)),
        _page(_table_block([header, ["项目丙", "枝江"]], continuation=True, page_index=2)),
    ]

    merge_table(pages)

    html = pages[0]["para_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["html"]
    rows = BeautifulSoup(html, "html.parser").find_all("tr")
    assert [[cell.get_text() for cell in row.find_all(["td", "th"])] for row in rows] == [
        header,
        ["项目甲", "宜昌"],
        ["项目乙", "宜都"],
        ["项目丙", "枝江"],
    ]
    assert len(pages[1]["para_blocks"]) == 1
    assert len(pages[2]["para_blocks"]) == 1
    assert all(
        block.get("lines_deleted")
        for page in pages[1:]
        for block in page["para_blocks"][0]["blocks"]
    )
