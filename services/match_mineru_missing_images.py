from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None


@dataclass
class ImageMeta:
    index: int
    name: str
    path: str
    width: Optional[int] = None
    height: Optional[int] = None
    size_bytes: Optional[int] = None

    @property
    def aspect_ratio(self) -> Optional[float]:
        if not self.width or not self.height:
            return None
        return self.width / self.height


@dataclass
class Slot:
    page_index: int
    block_index: int
    block_type: str
    bbox: Optional[list[float]]
    current_path: str

    @property
    def aspect_ratio(self) -> Optional[float]:
        if not self.bbox or len(self.bbox) != 4:
            return None
        x1, y1, x2, y2 = self.bbox
        w = max(1.0, float(x2) - float(x1))
        h = max(1.0, float(y2) - float(y1))
        return w / h

    @property
    def area(self) -> Optional[float]:
        if not self.bbox or len(self.bbox) != 4:
            return None
        x1, y1, x2, y2 = self.bbox
        return max(1.0, float(x2) - float(x1)) * max(1.0, float(y2) - float(y1))


def _load_json(json_text: str) -> Any:
    if isinstance(json_text, str):
        return json.loads(json_text)
    raise TypeError("json_text must be a JSON string")


def _iter_blocks(parsed: Any) -> Iterable[tuple[int, int, dict[str, Any]]]:
    if not isinstance(parsed, list):
        raise ValueError("Expected MinerU content-list JSON to be a top-level list of pages")
    for page_index, page in enumerate(parsed, start=1):
        if not isinstance(page, list):
            continue
        for block_index, block in enumerate(page, start=1):
            if isinstance(block, dict):
                yield page_index, block_index, block


def _block_image_path(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, dict):
        image_source = content.get("image_source")
        if isinstance(image_source, dict):
            path = image_source.get("path")
            if isinstance(path, str):
                return path
    return ""


def _set_block_image_path(block: dict[str, Any], new_path: str) -> None:
    content = block.setdefault("content", {})
    if not isinstance(content, dict):
        return
    image_source = content.setdefault("image_source", {})
    if isinstance(image_source, dict):
        image_source["path"] = new_path


def _get_block_bbox(block: dict[str, Any]) -> Optional[list[float]]:
    bbox = block.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4 and all(isinstance(x, (int, float)) for x in bbox):
        return [float(x) for x in bbox]
    return None


def _collect_slots(parsed: Any) -> list[Slot]:
    slots: list[Slot] = []
    for page_index, block_index, block in _iter_blocks(parsed):
        block_type = str(block.get("type", ""))
        path = _block_image_path(block)
        if block_type in {"table", "image", "figure"} and (path == "" or path.endswith("/")):
            slots.append(
                Slot(
                    page_index=page_index,
                    block_index=block_index,
                    block_type=block_type,
                    bbox=_get_block_bbox(block),
                    current_path=path,
                )
            )
    return slots


def _normalize_image_item(item: Any, index: int) -> ImageMeta:
    if isinstance(item, str):
        raw_path = item
        name = Path(raw_path).name
    elif isinstance(item, dict):
        raw_path = str(
            item.get("path")
            or item.get("image_path")
            or item.get("file")
            or item.get("name")
            or ""
        )
        name = str(item.get("name") or Path(raw_path).name or f"image_{index}")
    else:
        raw_path = str(item)
        name = Path(raw_path).name

    meta = ImageMeta(index=index, name=name, path=raw_path)

    # Allow callers to pass precomputed size metadata.
    if isinstance(item, dict):
        for key in ("width", "w"):
            if isinstance(item.get(key), int):
                meta.width = int(item[key])
                break
        for key in ("height", "h"):
            if isinstance(item.get(key), int):
                meta.height = int(item[key])
                break
        for key in ("size_bytes", "bytes", "length"):
            if isinstance(item.get(key), int):
                meta.size_bytes = int(item[key])
                break

    if (meta.width is None or meta.height is None) and meta.path:
        file_path = Path(meta.path)
        if file_path.exists() and Image is not None:
            try:
                with Image.open(file_path) as im:
                    meta.width, meta.height = im.size
            except Exception:
                pass
        if meta.size_bytes is None and file_path.exists():
            try:
                meta.size_bytes = file_path.stat().st_size
            except Exception:
                pass

    return meta


def _collect_referenced_paths(parsed: Any) -> set[str]:
    refs: set[str] = set()
    for _, _, block in _iter_blocks(parsed):
        path = _block_image_path(block)
        if path and not path.endswith("/"):
            refs.add(path)
    return refs


def _score(slot: Slot, image: ImageMeta, position_gap: int) -> float:
    score = 0.0

    slot_ar = slot.aspect_ratio
    img_ar = image.aspect_ratio
    if slot_ar is not None and img_ar is not None:
        score += abs(math.log(max(slot_ar, 1e-9) / max(img_ar, 1e-9))) * 4.0
    else:
        score += 1.5

    if slot.area is not None and image.width and image.height:
        img_area = float(image.width * image.height)
        if img_area > 0:
            # Use log-area distance, but keep it soft because MinerU crops are often not exact.
            score += abs(math.log(max(slot.area, 1.0) / img_area)) * 0.8

    if image.size_bytes:
        score += 0.05 * math.log(max(image.size_bytes, 1))

    # Prefer local ordering: in MinerU outputs, extracted images usually follow page order.
    score += 0.35 * max(0, position_gap)

    # Empty slots on later pages should rarely map to earlier images once the sequence advances.
    score += 0.15 * abs(position_gap)
    return score


def _assign_missing_images(
    parsed: Any,
    image_list: list[Any],
) -> tuple[Any, list[dict[str, Any]]]:
    MIN_CONFIDENCE = 0.35
    slots = _collect_slots(parsed)
    referenced = _collect_referenced_paths(parsed)
    images = [_normalize_image_item(item, idx) for idx, item in enumerate(image_list)]
    candidates = [img for img in images if img.path and img.path not in referenced]

    # Sort candidates by the order they were supplied. When dimensions are available,
    # keep this stable and only use scoring to choose among still-unassigned items.
    assigned: list[dict[str, Any]] = []
    used: set[int] = set()

    for slot_idx, slot in enumerate(slots):
        scored: list[tuple[float, ImageMeta, int]] = []
        for cand in candidates:
            if cand.index in used:
                continue
            position_gap = cand.index - slot_idx
            s = _score(slot, cand, position_gap)
            scored.append((s, cand, position_gap))

        scored.sort(key=lambda x: x[0])
        top_candidates = [
            {
                "image": asdict(cand),
                "score": round(score, 6),
                "confidence": round(1.0 / (1.0 + score), 6),
                "position_gap": position_gap,
            }
            for score, cand, position_gap in scored[:5]
        ]

        if not scored:
            assigned.append(
                {
                    "page_index": slot.page_index,
                    "block_index": slot.block_index,
                    "block_type": slot.block_type,
                    "matched_image": None,
                    "confidence": 0.0,
                    "candidates": [],
                }
            )
            continue

        best_score, best_cand, position_gap = scored[0]
        best_confidence = 1.0 / (1.0 + best_score)
        if best_confidence < MIN_CONFIDENCE:
            assigned.append(
                {
                    "page_index": slot.page_index,
                    "block_index": slot.block_index,
                    "block_type": slot.block_type,
                    "slot_bbox": slot.bbox,
                    "matched_image": None,
                    "confidence": round(best_confidence, 6),
                    "position_gap": position_gap,
                    "candidates": top_candidates,
                }
            )
            continue

        used.add(best_cand.index)

        page = parsed[slot.page_index - 1]
        block = page[slot.block_index - 1]
        _set_block_image_path(block, best_cand.path)

        assigned.append(
            {
                "page_index": slot.page_index,
                "block_index": slot.block_index,
                "block_type": slot.block_type,
                "slot_bbox": slot.bbox,
                "matched_image": asdict(best_cand),
                "confidence": round(best_confidence, 6),
                "position_gap": position_gap,
                "candidates": top_candidates,
            }
        )

    return parsed, assigned


def match_missing_images(json_text: str, image_list: list[Any]) -> dict[str, Any]:
    """
    Match missing MinerU image slots to image files.

    Parameters
    ----------
    json_text:
        The raw MinerU JSON text.
    image_list:
        A list of image paths or dict-like metadata entries. Supported dict keys:
        path/name/image_path/file, width/w, height/h, size_bytes/bytes/length.

    Returns
    -------
    dict with:
      - patched_json: JSON-serializable object with missing paths filled when possible
      - matches: list of slot -> image assignments
      - missing_slots: slots that still could not be matched
    """
    parsed = _load_json(json_text)
    patched, matches = _assign_missing_images(parsed, image_list)

    missing_slots = [m for m in matches if m.get("matched_image") is None]
    return {
        "patched_json": patched,
        "matches": matches,
        "missing_slots": missing_slots,
    }


def match_missing_images_text(json_text: str, image_list: list[Any], indent: int = 2) -> str:
    """
    Convenience wrapper that returns the patched JSON as a string.
    """
    result = match_missing_images(json_text, image_list)
    return json.dumps(result["patched_json"], ensure_ascii=False, indent=indent)


if __name__ == "__main__":
    # Minimal CLI for local debugging:
    #   python match_mineru_missing_images.py input.json
    import sys

    if len(sys.argv) < 2:
        raise SystemExit("Usage: python match_mineru_missing_images.py input.json")

    input_json = Path(sys.argv[1]).read_text(encoding="utf-8")
    image_dir = Path("images")
    image_list = [str(p) for p in sorted(image_dir.glob("*"))]
    out = match_missing_images(input_json, image_list)
    print(json.dumps(out["matches"], ensure_ascii=False, indent=2))
