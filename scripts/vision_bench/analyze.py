"""Score benchmark detections: accuracy, Wilson CI, per-class recall, McNemar.

    .venv/bin/python scripts/vision_bench/analyze.py
"""

import math
from collections import defaultdict
from typing import Dict, List, Tuple

from common import BENCH_DIR, DETECTIONS_PATH, read_jsonl

TASK_KEYS = {"role": "role", "scene": "scene", "vehicle_type": "vehicle_type"}


def wilson(ok: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = ok / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_p(b: int, c: int) -> float:
    total = b + c
    if total == 0:
        return 1.0
    tail = sum(math.comb(total, i) for i in range(0, min(b, c) + 1)) / (2 ** total)
    return min(1.0, 2 * tail)


def is_correct(record: Dict) -> bool:
    key = TASK_KEYS[record["task"]]
    data = record.get("data") or {}
    predicted = str(data.get(key, "")).strip().lower()
    return record.get("ok") and predicted == str(record.get("expected", "")).strip().lower()


def median(values: List[int]) -> int:
    values = sorted(values)
    if not values:
        return 0
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) // 2


def main() -> None:
    detections = [r for r in read_jsonl(DETECTIONS_PATH) if r.get("model")]
    if not detections:
        raise SystemExit("no detections yet - run run_eval.py first")
    latest: Dict[Tuple[str, str, str], Dict] = {}
    for record in detections:
        latest[(record["model"], record["task"], record["path"])] = record

    rows = defaultdict(list)
    for record in latest.values():
        rows[(record["task"], record["model"])].append(record)

    lines: List[str] = ["# Vision benchmark", ""]
    for task in ("role", "scene", "vehicle_type"):
        task_rows = {model: recs for (t, model), recs in rows.items() if t == task}
        if not task_rows:
            continue
        lines += [f"## {task}", "", "| model | n | correct | accuracy | 95% CI | invalid | median ms |", "|---|---|---|---|---|---|---|"]
        print(f"\n## {task}")
        print(f"{'model':20s} {'n':>4s} {'ok':>4s} {'acc':>6s} {'95% CI':>14s} {'invalid':>7s} {'p50ms':>6s}")
        for model, recs in sorted(task_rows.items()):
            n = len(recs)
            ok = sum(1 for r in recs if is_correct(r))
            invalid = sum(1 for r in recs if not r.get("ok"))
            low, high = wilson(ok, n)
            rows_line = f"| {model} | {n} | {ok} | {ok / n:.3f} | {low:.3f}-{high:.3f} | {invalid} | {median([r.get('ms', 0) for r in recs])} |"
            lines.append(rows_line)
            print(f"{model:20s} {n:4d} {ok:4d} {ok / n:6.3f} {f'{low:.3f}-{high:.3f}':>14s} {invalid:7d} {median([r.get('ms', 0) for r in recs]):6d}")

        if task == "role":
            classes = sorted({r["expected"] for r in task_rows[next(iter(task_rows))]})
            lines += ["", "| model | " + " | ".join(classes) + " |", "|---|" + "---|" * len(classes)]
            print("per-class recall")
            for model, recs in sorted(task_rows.items()):
                cells = []
                for cls in classes:
                    subset = [r for r in recs if r["expected"] == cls]
                    cells.append(f"{sum(1 for r in subset if is_correct(r)) / len(subset):.2f}" if subset else "-")
                lines.append(f"| {model} | " + " | ".join(cells) + " |")
                print(f"  {model:20s} " + " ".join(f"{cls[:12]}={cell:>4s}" for cls, cell in zip(classes, cells)))

        models = sorted(task_rows)
        pairs = []
        for i, model_a in enumerate(models):
            for model_b in models[i + 1:]:
                by_path_a = {r["path"]: is_correct(r) for r in task_rows[model_a]}
                by_path_b = {r["path"]: is_correct(r) for r in task_rows[model_b]}
                common = sorted(set(by_path_a) & set(by_path_b))
                b = sum(1 for p in common if by_path_a[p] and not by_path_b[p])
                c = sum(1 for p in common if not by_path_a[p] and by_path_b[p])
                pairs.append((model_a, model_b, len(common), b, c, mcnemar_p(b, c)))
        if pairs:
            print("McNemar (A better / B better / p)")
            lines += ["", "| pair | n | A only | B only | p |", "|---|---|---|---|---|"]
            for model_a, model_b, n, b, c, p in pairs:
                print(f"  {model_a} vs {model_b}: n={n} {b}/{c} p={p:.4f}")
                lines.append(f"| {model_a} vs {model_b} | {n} | {b} | {c} | {p:.4f} |")

    output = BENCH_DIR / "report.md"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {output}")


if __name__ == "__main__":
    main()
