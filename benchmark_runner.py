import os
import glob
import time
import json
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
    # initialize vision client with Gemini & DeepSeek API keys from environment
    gemini_key = os.environ.get("GEMINI_API_KEY")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    client = DualModelVisionClient(
        gemini_api_key=gemini_key,
        deepseek_api_key=deepseek_key,
        deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    )

    results = []
    for idx, fpath in enumerate(files):
        t0 = time.perf_counter()
        rel_name = os.path.relpath(fpath, DATASET_DIR)
        print(f"[{idx+1}/{len(files)}] comparing Gemini vs DeepSeek on {rel_name}...")

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

        # prepare messages payload for vision extraction
        import base64
        b64_img = base64.b64encode(webp_bytes).decode("utf-8")
        messages = [
            {
                "role": "system",
                "content": (
                    "extract visual facts from this survey/receipt photo strictly into JSON format. "
                    "output fields: vendor_name, tax_id, receipt_serial, line_items [{item_name, amount}], "
                    "raw_subtotal (int), vat_amount (int), raw_total (int), has_signature (bool), "
                    "unchecked_boxes_count (int), is_screen_photo (bool), is_manipulated_or_photoshopped (bool), "
                    "tamper_evidence (string or null), notes (string or null)."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "extract document facts:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/webp;base64,{b64_img}", "detail": "high"}},
                ],
            },
        ]

        # 4a. Gemini 3.5 Flash run
        t_gemini_start = time.perf_counter()
        gemini_facts = None
        gemini_err = None
        try:
            if client.gemini_client:
                gemini_facts = await client._call_gemini(messages)
            else:
                gemini_err = "GEMINI_API_KEY not set"
        except Exception as e:
            gemini_err = str(e)
        t_gemini = (time.perf_counter() - t_gemini_start) * 1000

        # 4b. DeepSeek Flash run
        t_ds_start = time.perf_counter()
        ds_facts = None
        ds_err = None
        try:
            if client.deepseek_client:
                ds_facts = await client._call_deepseek(messages)
            else:
                ds_err = "DEEPSEEK_API_KEY not configured in env"
        except Exception as e:
            ds_err = str(e)
        t_ds = (time.perf_counter() - t_ds_start) * 1000

        # 5. deterministic evaluation on primary facts
        primary_facts = ds_facts or gemini_facts
        eval_res = None
        if primary_facts:
            if is_tampered_forensic and not primary_facts.is_manipulated_or_photoshopped:
                primary_facts.is_manipulated_or_photoshopped = True
                primary_facts.tamper_evidence = tamper_reason
            eval_res = evaluate_survey_compliance(primary_facts)

        total_ms = (time.perf_counter() - t0) * 1000
        verdict = eval_res.reason_code if eval_res else "REJECTED_BLUR"

        results.append({
            "file": rel_name,
            "raw_size_kb": round(file_size_kb, 1),
            "webp_size_kb": round(len(webp_bytes) / 1024, 1),
            "blur_variance": round(blur_var, 1),
            "is_sharp": is_sharp,
            "is_tampered_forensic": is_tampered_forensic,
            "gemini_3_5_flash": {
                "latency_ms": round(t_gemini, 1),
                "facts": gemini_facts.model_dump() if gemini_facts else None,
                "error": gemini_err,
            },
            "deepseek_flash": {
                "latency_ms": round(t_ds, 1),
                "facts": ds_facts.model_dump() if ds_facts else None,
                "error": ds_err,
            },
            "verdict": verdict,
            "total_ms": round(total_ms, 1),
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
