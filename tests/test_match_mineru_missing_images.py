import json

from services.match_mineru_missing_images import match_missing_images


def _table(path, bbox):
    return {
        "type": "table",
        "content": {"image_source": {"path": path}},
        "bbox": bbox,
    }


def test_page_provenance_beats_identical_geometry_from_another_page():
    document = [
        [_table("images/first.jpg", [0, 0, 100, 100])],
        [_table("images/", [0, 0, 100, 100])],
    ]
    images = [
        {"path": "images/first.jpg", "width": 100, "height": 100,
         "page_index": 1, "block_type": "table", "bbox": [0, 0, 100, 100]},
        {"path": "images/wrong.jpg", "width": 100, "height": 100,
         "page_index": 99, "block_type": "table", "bbox": [0, 0, 100, 100]},
        {"path": "images/right.jpg", "width": 100, "height": 100,
         "page_index": 2, "block_type": "table", "bbox": [0, 0, 100, 100]},
    ]

    result = match_missing_images(json.dumps(document), images)

    assert result["matches"][0]["matched_image"]["path"] == "images/right.jpg"
    sources = result["patched_json"][0][0]["content"]["image_sources"]
    assert [source["path"] for source in sources] == [
        "images/first.jpg", "images/right.jpg"
    ]


def test_empty_table_does_not_attach_across_unrelated_pages():
    document = [
        [_table("images/old.jpg", [0, 0, 100, 100])],
        [{"type": "text", "content": "unrelated"}],
        [_table("images/", [0, 0, 100, 100])],
    ]
    images = [
        {"path": "images/old.jpg", "page_index": 1},
        {"path": "images/current.jpg", "page_index": 3,
         "block_type": "table", "bbox": [0, 0, 100, 100],
         "width": 100, "height": 100},
    ]

    result = match_missing_images(json.dumps(document), images)

    assert result["patched_json"][2][0]["content"]["image_source"]["path"] == "images/current.jpg"
    assert "image_sources" not in result["patched_json"][0][0]["content"]


def test_continuation_after_empty_intermediate_page_attaches_to_original_table():
    document = [
        [{
            **_table("images/first.jpg", [100, 800, 895, 920]),
            "content": {
                "image_source": {"path": "images/first.jpg"},
                "html": "<table><tr><td>first page rows</td></tr></table>",
                "table_caption": [{"type": "text", "content": "Table 25"}],
            },
        }],
        [{
            **_table("images/", [104, 118, 894, 921]),
            "content": {"image_source": {"path": "images/"}, "html": "", "table_caption": []},
        }],
        [{
            **_table("images/", [102, 116, 895, 339]),
            "content": {"image_source": {"path": "images/"}, "html": "", "table_caption": []},
        }],
    ]
    images = [
        {"path": "images/first.jpg", "page_index": 1, "width": 1312, "height": 223},
        {"path": "images/second.jpg", "page_index": 2, "width": 1306, "height": 1801},
        {"path": "images/third.jpg", "page_index": 3, "width": 1312, "height": 501},
    ]

    result = match_missing_images(json.dumps(document), images)

    first_sources = result["patched_json"][0][0]["content"]["image_sources"]
    assert [source["path"] for source in first_sources] == [
        "images/first.jpg", "images/second.jpg", "images/third.jpg"
    ]
    assert result["patched_json"][2][0]["content"]["image_source"]["path"] == "images/"
