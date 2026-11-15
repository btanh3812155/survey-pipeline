"""Shared helpers for the vision benchmark scripts.

Run from the repo root with the project venv, e.g.:

    PYTHONPATH=. .venv/bin/python scripts/vision_bench/label_dataset.py
    PYTHONPATH=. .venv/bin/python scripts/vision_bench/run_eval.py --limit-per-class 120
    .venv/bin/python scripts/vision_bench/analyze.py

Artifacts live under data/vision_bench/ (gitignored): labels.jsonl for the
deterministic ground truth and detections.jsonl for model responses. Both are
append-only caches keyed by file path + mtime, so reruns are free.
"""

import base64
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

BENCH_DIR = REPO_ROOT / "data" / "vision_bench"
LABELS_PATH = BENCH_DIR / "labels.jsonl"
DETECTIONS_PATH = BENCH_DIR / "detections.jsonl"
DEFAULT_DATASET = (
    REPO_ROOT / "(GSM) 16th Oct 26-20261031T084345Z-1-001" / "(GSM) 16th Oct 26"
)

# Vision-capable ids verified via each provider's /models endpoint (2026-11).
PROVIDERS = {
    "deepseek-flash": ("DEEPSEEK_API_KEY", "https://api.deepseek.com"),
    "gemini-2.5-flash": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    "gemini-3.5-flash": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    "gemini-3.8-flash": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/"),
}
DEFAULT_MODELS = list(PROVIDERS)


def load_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def client_for(model: str, env: Dict[str, str]):
    from openai import AsyncOpenAI

    key_name, base_url = PROVIDERS[model]
    api_key = env.get(key_name) or __import__("os").environ.get(key_name)
    if not api_key:
        raise RuntimeError(f"{key_name} is not configured")
    return AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)


def normalize_text(value: str) -> str:
    """Lowercase, strip Vietnamese diacritics and punctuation for OCR matching."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def file_signature(path: Path) -> str:
    stat = path.stat()
    return f"{path}:{stat.st_size}:{int(stat.st_mtime)}"


def parse_json_response(text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def image_data_url(path: Path, max_dim: int = 1024) -> str:
    import io

    from PIL import Image

    from services.normalizer import sanitize_and_normalize_image

    # Phone originals here reach 48MP; PIL's decompression-bomb guard rejects them.
    # The client compresses before upload in production, so mirror that here.
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((2048, 2048))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
    normalized, _ = sanitize_and_normalize_image(buffer.getvalue(), max_dimension=max_dim)
    return "data:image/webp;base64," + base64.b64encode(normalized).decode()


async def ask_json(
    client,
    model: str,
    system: str,
    user_text: str,
    image_url: str,
    timeout: float = 60.0,
    max_tokens: int = 512,
) -> Tuple[bool, Optional[Dict[str, Any]], int, Optional[str]]:
    started = time.time()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": image_url, "detail": "auto"}},
                    ],
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        elapsed = int((time.time() - started) * 1000)
        return True, parse_json_response(response.choices[0].message.content), elapsed, None
    except Exception as exc:  # network / provider errors are recorded, not raised
        elapsed = int((time.time() - started) * 1000)
        return False, None, elapsed, str(exc)[:200]
