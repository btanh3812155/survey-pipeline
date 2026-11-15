"""Build deterministic ground-truth labels for the vision benchmark.

Labels come from tesseract OCR (eng model, which reads Vietnamese screens well
enough for keyword matching on the diacritic-stripped text) and image geometry.
No LLM under test is used for labelling.

Only app-screen roles are inferred automatically (app_income / app_profile).
Object roles (vehicle_exterior, odometer) cannot be told apart from dark or
low-text screen photos without real metadata, so they stay "unknown" here and
are excluded from scoring. Point --dataset at a collection with known object
labels to benchmark object detection.

    PYTHONPATH=. .venv/bin/python scripts/vision_bench/label_dataset.py --workers 8
"""

import argparse
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PIL import Image

from common import BENCH_DIR, DEFAULT_DATASET, LABELS_PATH, append_jsonl, file_signature, normalize_text, read_jsonl

# Menu/header markers unique to the driver profile/utility screen.
PROFILE_MARKERS = (
    "tienichbotro",
    "bandohotro",
    "kiemtraantoan",
    "congvieccuatoi",
    "hoso",
    "thongtincanhan",
    "lichsuthanhtich",
    "hangthanhvien",
    "xanhpoints",
    "mathaixe",
    "mataixe",
    "dangthongtin",
    "vitaixe",
)
# Numeric-row markers unique to the weekly income screen.
INCOME_MARKERS = ("doanhso", "chietkhau", "thunhaprong", "tongsochuyen")
AMOUNT_RE = re.compile(r"\d{1,3}(?:[.,]\d{3}){1,3}\s*[dđ]?")


def _ocr_image(image: Image.Image) -> str:
    handle, tmp_path = tempfile.mkstemp(suffix=".jpg")
    os.close(handle)
    try:
        image.save(tmp_path, quality=88)
        result = subprocess.run(
            ["tesseract", tmp_path, "stdout", "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.stdout or ""
    finally:
        os.unlink(tmp_path)


def ocr_text(image_path: Path, max_dim: int = 1600) -> str:
    with Image.open(image_path) as image:
        image = image.convert("RGB")
        image.thumbnail((max_dim, max_dim))
        texts = [_ocr_image(image)]
        if image.width > image.height * 1.05:
            # drivers often photograph the phone sideways; OCR both landscape rotations
            texts.append(_ocr_image(image.rotate(-90, expand=True)))
            texts.append(_ocr_image(image.rotate(90, expand=True)))
        return "\n".join(texts)


def classify(text: str, width: int, height: int, folder_name: str) -> Tuple[str, Optional[str]]:
    normalized = normalize_text(text)
    # tesseract often prints Vietnamese "ô" as the digit 6; fold it for keyword matching.
    folded = normalized.replace("6", "o")
    vehicle_class = None
    if " - " in folder_name:
        candidate = folder_name.rsplit(" - ", 1)[-1].strip().lower()
        if candidate in ("car", "bike"):
            vehicle_class = candidate

    def has(markers) -> bool:
        return any(marker in normalized or marker in folded for marker in markers)

    income_hit = has(INCOME_MARKERS)
    if not income_hit:
        amounts = AMOUNT_RE.findall(text or "")
        if len(amounts) >= 2 and "tuan" in normalized and ("thunhap" in normalized or "tengthunhap" in normalized or "tongthunhap" in folded):
            income_hit = True

    if len(normalized) < 12:
        # too little text to classify; could be a dark screen or a real object.
        # object roles need explicit metadata, not an OCR guess.
        role = "unknown"
    elif income_hit:
        role = "app_income"
    elif has(PROFILE_MARKERS):
        role = "app_profile"
    else:
        role = "unknown"
    return role, vehicle_class if role == "vehicle_exterior" else None


def label_one(image_path: Path, dataset: Path, ocr_cache: Dict[str, str], metadata: Dict[str, Any]):
    try:
        with Image.open(image_path) as image:
            width, height = image.size
        relative = str(image_path.relative_to(dataset))
        signature = f"v2:{file_signature(image_path)}"
        override = metadata.get(relative)
        if override is not None:
            entry = override if isinstance(override, dict) else {"role": str(override)}
            return {
                "path": relative,
                "signature": signature,
                "role": entry.get("role", "unknown"),
                "vehicle_class": entry.get("vehicle_class"),
                "width": width,
                "height": height,
                "ocr_head": "",
                "source": "metadata",
            }, None
        text = ocr_cache.get(signature)
        new_ocr = None
        if text is None:
            text = ocr_text(image_path)
            new_ocr = (signature, text)
            ocr_cache[signature] = text
        role, vehicle_class = classify(text, width, height, image_path.parent.name)
        head = " ".join(line.strip() for line in text.splitlines() if line.strip())[:120]
        return {
            "path": relative,
            "signature": signature,
            "role": role,
            "vehicle_class": vehicle_class,
            "width": width,
            "height": height,
            "ocr_head": head,
            "source": "ocr",
        }, new_ocr
    except Exception as exc:
        return {"path": str(image_path), "error": str(exc)[:160]}, None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--samples", type=int, default=3, help="examples printed per role")
    parser.add_argument("--metadata", type=Path, default=None, help='JSON {"relative/path": "role" or {"role": ..., "vehicle_class": ...}}')
    args = parser.parse_args()

    metadata = json.loads(args.metadata.read_text(encoding="utf-8")) if args.metadata else {}
    paths = sorted(p for p in args.dataset.rglob("*") if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    existing = {record["path"]: record for record in read_jsonl(LABELS_PATH) if "path" in record and "signature" in record}
    ocr_cache = {record["signature"]: record["text"] for record in read_jsonl(BENCH_DIR / "ocr.jsonl") if "signature" in record}
    todo = [p for p in paths if existing.get(str(p.relative_to(args.dataset)), {}).get("signature") != f"v2:{file_signature(p)}"]
    print(f"dataset={args.dataset} images={len(paths)} cached={len(paths) - len(todo)} to_label={len(todo)} ocr_cached={len(ocr_cache)} metadata={len(metadata)}")

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for record, new_ocr in pool.map(lambda p: label_one(p, args.dataset, ocr_cache, metadata), todo):
            if new_ocr:
                append_jsonl(BENCH_DIR / "ocr.jsonl", {"signature": new_ocr[0], "text": new_ocr[1]})
            append_jsonl(LABELS_PATH, record)
            existing[record["path"]] = record
            done += 1
            if done % 50 == 0:
                print(f"  labelled {done}/{len(todo)}")

    counts: Dict[str, int] = {}
    for record in existing.values():
        counts[record.get("role", "error")] = counts.get(record.get("role", "error"), 0) + 1
    print("labels:", dict(sorted(counts.items(), key=lambda kv: -kv[1])))
    for role in ("app_income", "app_profile", "vehicle_exterior", "unknown"):
        examples = [r for r in existing.values() if r.get("role") == role][: args.samples]
        for example in examples:
            print(f"  [{role}] {example['path']} :: {example.get('ocr_head', '')[:90]}")
    print(f"wrote {LABELS_PATH}")


if __name__ == "__main__":
    main()
