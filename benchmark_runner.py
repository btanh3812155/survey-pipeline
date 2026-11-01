import os
import glob
import time
import json
from PIL import Image
from services.normalizer import (
    sanitize_and_normalize_image,
    compute_laplacian_blur_score,
    detect_image_manipulation,
)
from services.vision_ai import DualModelVisionClient
from services.evaluator import evaluate_survey_compliance

# directory containing GSM sample images
DATASET_DIR = "/home/ebylxn/Downloads/(GSM) 16th Oct 26"


def sample_representative_images(max_samples: int = 15):
    all_files = sorted(glob.glob(os.path.join(DATASET_DIR, "*", "*.jpg")))
    if not all_files:
        return []
    # pick evenly spaced samples across bike and car inspection categories
    step = max(1, len(all_files) // max_samples)
    return all_files[::step][:max_samples]


async def run_benchmark():
    files = sample_representative_images(15)
    print(f"selected {len(files)} representative images from GSM dataset")

    # initialize vision client with Gemini API key from environment
    gemini_key = os.environ.get("GEMINI_API_KEY")
    client = DualModelVisionClient(gemini_api_key=gemini_key)

    results = []
    for idx, fpath in enumerate(files):
        t0 = time.perf_counter()
        rel_name = os.path.relpath(fpath, DATASET_DIR)
        print(f"[{idx+1}/{len(files)}] processing {rel_name}...")

        with open(fpath, "rb") as f:
            raw_bytes = f.read()

        file_size_kb = len(raw_bytes) / 1024

        # 1. normalize & sanitize
        t_norm_start = time.perf_counter()
        webp_bytes, pil_img = sanitize_and_normalize_image(raw_bytes)
        t_norm = (time.perf_counter() - t_norm_start) * 1000

        # 2. blur check
        is_sharp, blur_var = compute_laplacian_blur_score(pil_img)

        # 3. photoshop / tamper check (forensic metadata + ELA)
        is_tampered_forensic, tamper_reason = detect_image_manipulation(raw_bytes, pil_img)

        # 4. vision AI extraction
        t_ai_start = time.perf_counter()
        ai_error = None
        facts = None
        try:
            facts = await client.extract_facts(webp_bytes)
        except Exception as e:
            ai_error = str(e)
            print(f"   [!] AI Extraction failed for {rel_name}: {e}")
        t_ai = (time.perf_counter() - t_ai_start) * 1000
        # 5. deterministic evaluation
        eval_res = None
        if facts:
            # combine forensic tamper detection with AI tamper flag
            if is_tampered_forensic and not facts.is_manipulated_or_photoshopped:
                facts.is_manipulated_or_photoshopped = True
                facts.tamper_evidence = tamper_reason
            eval_res = evaluate_survey_compliance(facts)

        total_ms = (time.perf_counter() - t0) * 1000
        verdict = eval_res.reason_code if eval_res else ("AI_ERROR" if ai_error else "REJECTED_BLUR")
        print(f"   -> Verdict: {verdict} | Total: {total_ms:.1f}ms (Norm: {t_norm:.1f}ms, AI: {t_ai:.1f}ms)")
        results.append({
            "file": rel_name,
            "raw_size_kb": round(file_size_kb, 1),
            "webp_size_kb": round(len(webp_bytes) / 1024, 1),
            "blur_variance": round(blur_var, 1),
            "is_sharp": is_sharp,
            "is_tampered_forensic": is_tampered_forensic,
            "tamper_reason": tamper_reason,
            "facts": facts.model_dump() if facts else None,
            "verdict": verdict,
            "ai_error": ai_error,
            "timing_ms": {
                "normalization": round(t_norm, 1),
                "ai_inference": round(t_ai, 1),
                "total": round(total_ms, 1),
            }
        })
        if idx < len(files) - 1:
            await asyncio.sleep(1.0)

    report_path = os.path.join(os.path.dirname(__file__), "benchmark_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"benchmark complete. saved {len(results)} image evaluations to {report_path}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_benchmark())
