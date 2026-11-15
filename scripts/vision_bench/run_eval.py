"""Run the vision models over the labelled dataset and cache every response.

Tasks
  role          app_income / app_profile / vehicle_exterior / unknown
  scene         phone app screen vs real-world photo
  vehicle_type  car vs bike (vehicle_exterior images only)

    PYTHONPATH=. .venv/bin/python scripts/vision_bench/run_eval.py --limit-per-class 120

Responses land in data/vision_bench/detections.jsonl keyed by model+task+file
signature, so reruns and partial runs only pay for what is missing.
"""

import argparse
import asyncio
from pathlib import Path
from typing import Dict, List

from common import (
    DETECTIONS_PATH,
    DEFAULT_DATASET,
    DEFAULT_MODELS,
    LABELS_PATH,
    append_jsonl,
    ask_json,
    client_for,
    file_signature,
    image_data_url,
    load_env,
    read_jsonl,
)

ROLE_SYSTEM = (
    "You classify photos submitted to a Vietnamese ride-hailing driver survey. "
    "Return exactly one role: app_income (weekly earnings/income statistics screen), "
    "app_profile (driver profile or utility menu screen), vehicle_exterior (real-world photo of a car or motorcycle), "
    "or unknown. Return strict JSON: {\"role\": \"<role>\"}."
)
SCENE_SYSTEM = (
    "Decide whether the image is a phone application screen (screenshot or photo of a phone UI) "
    "or a real-world scene/photo. Return strict JSON: {\"scene\": \"screen\"} or {\"scene\": \"photo\"}."
)
VEHICLE_SYSTEM = (
    "Look at the vehicle in the photo. Classify it as a car (including taxis/SUVs) or a motorcycle/scooter/bicycle. "
    "Return strict JSON: {\"vehicle_type\": \"car\"} or {\"vehicle_type\": \"bike\"}."
)
TASKS = {
    "role": (ROLE_SYSTEM, "What role is this image?", {"role"}),
    "scene": (SCENE_SYSTEM, "Is this a phone app screen or a real-world photo?", {"scene"}),
    "vehicle_type": (VEHICLE_SYSTEM, "Car or motorcycle?", {"vehicle_type"}),
}


def expected_label(task: str, label: Dict) -> str:
    if task == "role":
        return label["role"]
    if task == "scene":
        return "screen" if label["role"] in ("app_income", "app_profile") else "photo"
    return label.get("vehicle_class") or ""


def pick_cases(labels: List[Dict], task: str, limit_per_class: int) -> List[Dict]:
    buckets: Dict[str, List[Dict]] = {}
    for label in labels:
        if label.get("role") == "unknown":
            # our deterministic labeler cannot pin these down; scoring them would
            # penalise models for our own label noise
            continue
        expected = expected_label(task, label)
        if not expected:
            continue
        buckets.setdefault(expected, []).append(label)
    cases: List[Dict] = []
    for expected, items in sorted(buckets.items()):
        items.sort(key=lambda item: item["path"])
        if limit_per_class and len(items) > limit_per_class:
            stride = len(items) / float(limit_per_class)
            items = [items[int(i * stride)] for i in range(limit_per_class)]
        cases.extend(dict(label, expected=expected) for label in items)
    return cases


async def run_task(model: str, client, task: str, cases: List[Dict], dataset: Path, cache: Dict, concurrency: int, retries: int) -> None:
    system, user_text, keys = TASKS[task]
    semaphore = asyncio.Semaphore(concurrency)
    done = 0

    async def handle(case: Dict) -> None:
        nonlocal done
        path = dataset / case["path"]
        signature = case["signature"]
        cache_key = (model, task, case["path"], signature)
        if cache_key in cache:
            return
        url = image_data_url(path)
        async with semaphore:
            attempt = 0
            while True:
                ok, data, elapsed, error = await ask_json(client, model, system, user_text, url)
                if ok and data and keys & set(data):
                    break
                attempt += 1
                if attempt > retries:
                    break
                await asyncio.sleep(1.5 * attempt)
        record = {
            "model": model,
            "task": task,
            "path": case["path"],
            "signature": signature,
            "expected": case["expected"],
            "ok": bool(ok and data and keys & set(data)),
            "data": data,
            "ms": elapsed,
            "error": error,
        }
        append_jsonl(DETECTIONS_PATH, record)
        cache[cache_key] = record
        done += 1
        if done % 100 == 0:
            print(f"  {model}/{task}: {done}")

    await asyncio.gather(*(handle(case) for case in cases))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--tasks", default="role,scene,vehicle_type")
    parser.add_argument("--limit-per-class", type=int, default=120)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()

    labels = [r for r in read_jsonl(LABELS_PATH) if "role" in r]
    if not labels:
        raise SystemExit("no labels yet - run label_dataset.py first")
    cache = {(r["model"], r["task"], r["path"], r["signature"]): r for r in read_jsonl(DETECTIONS_PATH)}
    env = load_env()

    for task in [t.strip() for t in args.tasks.split(",") if t.strip()]:
        cases = pick_cases(labels, task, args.limit_per_class)
        by_expected: Dict[str, int] = {}
        for case in cases:
            by_expected[case["expected"]] = by_expected.get(case["expected"], 0) + 1
        print(f"task={task} cases={len(cases)} {by_expected}")
        for model in [m.strip() for m in args.models.split(",") if m.strip()]:
            pending = [c for c in cases if (model, task, c["path"], c["signature"]) not in cache]
            print(f"  {model}: {len(cases) - len(pending)} cached, {len(pending)} to run")
            if not pending:
                continue
            await run_task(model, client_for(model, env), task, cases, args.dataset, cache, args.concurrency, args.retries)
    print(f"wrote {DETECTIONS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
