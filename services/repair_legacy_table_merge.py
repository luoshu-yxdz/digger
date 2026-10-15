"""Repair multi-page table image links in an existing MinerU V2 parse directory."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from services.match_mineru_missing_images import match_missing_images


def _collect_middle_image_metadata(middle_path: Path) -> dict[str, list[dict[str, Any]]]:
    if not middle_path.is_file():
        return {}
    try:
        pages = json.loads(middle_path.read_text(encoding="utf-8")).get("pdf_info", [])
    except (OSError, ValueError, AttributeError):
        return {}

    metadata: dict[str, list[dict[str, Any]]] = {}
    for page_index, page in enumerate(pages, start=1):
        stack, seen = [page], set()
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                filename = value.get("image_path")
                bbox = value.get("bbox")
                block_type = value.get("type")
                if isinstance(filename, str):
                    key = (page_index, block_type, tuple(bbox) if isinstance(bbox, list) else None)
                    if key not in seen:
                        seen.add(key)
                        metadata.setdefault(filename, []).append({
                            "page_index": page_index,
                            "block_type": block_type,
                            "bbox": bbox,
                        })
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    return metadata


def _build_image_inventory(image_dir: Path, metadata: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    from PIL import Image

    inventory = []
    if not image_dir.is_dir():
        return inventory
    for path in image_dir.iterdir():
        if not path.is_file():
            continue
        item: dict[str, Any] = {
            "path": f"images/{path.name}",
            "size_bytes": path.stat().st_size,
        }
        try:
            with Image.open(path) as image:
                item.update(width=image.width, height=image.height)
        except Exception:
            pass
        for record in metadata.get(path.name) or [None]:
            enriched = dict(item)
            if record:
                enriched.update(record)
            inventory.append(enriched)
    return inventory


def _merge_table_images(parsed: list[Any], output_dir: Path) -> None:
    from PIL import Image

    for page in parsed:
        if not isinstance(page, list):
            continue
        for block in page:
            if not isinstance(block, dict) or block.get("type") != "table":
                continue
            content = block.get("content")
            sources = content.get("image_sources") if isinstance(content, dict) else None
            if not isinstance(sources, list) or len(sources) < 2:
                continue
            relative_paths = [
                source.get("path") for source in sources
                if isinstance(source, dict) and isinstance(source.get("path"), str)
            ]
            if len(relative_paths) != len(sources) or any(
                not path.startswith("images/") for path in relative_paths
            ):
                continue
            source_paths = [output_dir.joinpath(*path.split("/")) for path in relative_paths]
            if not all(path.is_file() for path in source_paths):
                continue

            digest = hashlib.sha1("|".join(relative_paths).encode("utf-8")).hexdigest()[:20]
            merged_name = f"{digest}_table_merged.jpg"
            merged_path = output_dir / merged_name
            if not merged_path.is_file():
                images = [Image.open(path).convert("RGB") for path in source_paths]
                try:
                    merged = Image.new(
                        "RGB",
                        (max(image.width for image in images), sum(image.height for image in images)),
                        "white",
                    )
                    y = 0
                    for image in images:
                        merged.paste(image, (0, y))
                        y += image.height
                    merged.save(merged_path, format="JPEG", quality=95)
                finally:
                    for image in images:
                        image.close()
            image_source = content.setdefault("image_source", {})
            if isinstance(image_source, dict):
                image_source["path"] = merged_name


def repair_legacy_table_merge(
    parse_dir: str | Path,
    pdf_name: str,
    output_path: str | Path | None = None,
) -> Path:
    """Repair an existing V2 result without rerunning PDF inference.

    The original V2 and V2.1 files are kept. By default, the repaired JSON is
    written beside them as ``*_content_list_v2_repaired.json``.
    """
    parse_dir = Path(parse_dir)
    v2_path = parse_dir / f"{pdf_name}_content_list_v2.json"
    if not v2_path.is_file():
        raise FileNotFoundError(v2_path)

    source_text = v2_path.read_text(encoding="utf-8")
    metadata = _collect_middle_image_metadata(parse_dir / f"{pdf_name}_middle.json")
    inventory = _build_image_inventory(parse_dir / "images", metadata)
    result = match_missing_images(source_text, inventory)
    parsed = result["patched_json"]
    _merge_table_images(parsed, parse_dir)

    output_path = Path(output_path) if output_path else parse_dir / f"{pdf_name}_content_list_v2_repaired.json"
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=4), encoding="utf-8")
    temporary_path.replace(output_path)
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parse_dir", help="Existing MinerU parse output directory")
    parser.add_argument("pdf_name", help="PDF stem used in the parse output filenames")
    parser.add_argument("--output", help="Optional repaired JSON output path")
    args = parser.parse_args()
    output = repair_legacy_table_merge(args.parse_dir, args.pdf_name, args.output)
    print(output)


if __name__ == "__main__":
    main()
