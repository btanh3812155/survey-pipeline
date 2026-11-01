# GSM Dataset Benchmark & Tamper Auditing Report

**Dataset**: GSM VinFast Car & Bike Survey Inspection Dataset (`/home/ebylxn/Downloads/(GSM) 16th Oct 26`)  
**Pipeline**: Vietnamese Survey & Receipt OCR/Compliance Pipeline (`/home/ebylxn/projects/survey-pipeline`)  
**Evaluator**: `benchmark_runner.py` with `DualModelVisionClient` (Gemini 3.5 Flash / Flash Lite)  
**Date**: November 1, 2026  

---

## 1. Executive Summary

This report documents the empirical benchmark results of running the automated document and vehicle survey processing pipeline against the GSM dataset collected in Vietnam on October 16–27, 2026. The full dataset encompasses **648 high-resolution images** across **108 inspection folders** (29 bike surveys, 79 car surveys), representing VinFast electric vehicle inspections and GSM driver partner performance surveys.

A representative subset of 15 images spanning bike and car inspections was systematically benchmarked for end-to-end processing throughput, image sanitization, compression efficiency, blur filtering, digital manipulation forensics (EXIF metadata and Error Level Analysis), and Gemini multimodal extraction precision. In addition, an exhaustive forensic scan was conducted across the entire 648-image dataset.

### Key Performance Highlights

| Metric | Target / Benchmark Result | Status |
| :--- | :--- | :--- |
| **Dataset Volume** | 648 images across 108 survey sessions | Verified |
| **WebP Compression Ratio** | **93.87% mean reduction** (2,172.9 KB raw $\to$ 49.4 KB WebP) | Exceeded target |
| **Normalization Latency (p50 / p95)** | **316.7 ms** / **347.8 ms** | Fast (< 350 ms) |
| **Gemini AI Latency (p50 / p95)** | **4,367.0 ms** / **6,391.6 ms** | Reliable |
| **Total Pipeline Latency (p50 / p95)**| **4,764.2 ms** / **6,758.3 ms** | Operational |
| **Blur Detection Gatekeeper** | Laplacian variance threshold $\ge 100.0$; filtered 15.7% of full dataset | Verified |
| **Tamper Detection (Full Dataset)** | 2 ELA splicing anomalies detected; 0 unauthorized software tags | Verified |
| **Anti-Spoofing Accuracy** | 100% of tested smartphone screen captures identified (`SCREEN_PHOTO`) | Verified |

---

## 2. Dataset Composition & Representative Sampling

The dataset contains survey records submitted by field surveyors (e.g., Đoàn Hải Yến, Vỹ Phan Triều) documenting VinFast electric bikes (Feliz S, Klara, Evo200) and cars (VF e34, VF 5 Plus, VF 8) operating under GSM (Xanh SM).

```
/home/ebylxn/Downloads/(GSM) 16th Oct 26/
├── 01 - Bike/ (7 images: app earnings, trip stats, profile)
├── 02 - Bike/ (2 images)
├── 101 - Car/ (6 images: smartphone captures of driver earnings, vehicle stats)
├── 108 - Bike/ (6 images)
...
└── 108 folders total (29 Bike, 79 Car; 648 total images: 543 .jpg, 62 .jpeg, 43 .JPG)
```

### Representative 15-Sample Benchmark Set

The benchmark runner sampled 15 evenly spaced items across the dataset:

| # | Image Relative Path | Category | Raw Size | WebP Size | Compression |
| :-: | :--- | :---: | :---: | :---: | :---: |
| 1 | `01 - Bike/IMG_1792069227891_1792121791578.jpg` | Bike | 105.7 KB | 23.8 KB | 77.5% |
| 2 | `101 - Car/IMG_20261027_162904 - Đoàn Hải Yến-SBD1.jpg` | Car | 2,033.7 KB | 47.5 KB | 97.7% |
| 3 | `108 - Bike/IMG_20261027_181535 - Đoàn Hải Yến-SBD1.jpg` | Bike | 4,337.2 KB | 63.4 KB | 98.5% |
| 4 | `16 - Bike/Bản sao của IMG_20261022_163814 - Đoàn Hải Yến-SBD1.jpg`| Bike | 2,256.9 KB | 46.1 KB | 98.0% |
| 5 | `24 - Bike/IMG_20261023_131306.jpg` | Bike | 2,277.5 KB | 49.4 KB | 97.8% |
| 6 | `32 - Bike/Bản sao của IMG_20261023_141111 - Đoàn Hải Yến-SBD1.jpg`| Bike | 1,827.2 KB | 26.3 KB | 98.6% |
| 7 | `37 - Car/Bản sao của IMG_20261023_152854 - Đoàn Hải Yến-SBD1.jpg` | Car | 2,477.9 KB | 56.6 KB | 97.7% |
| 8 | `44 - Car/IMG_1792810211413_1792819515001.jpg` | Car | 167.2 KB | 72.4 KB | 56.7% |
| 9 | `56 - Car/Bản sao của IMG_20261024_165536 - Đoàn Hải Yến-SBD1.jpg` | Car | 2,804.8 KB | 36.5 KB | 98.7% |
| 10 | `62 - Car/Bản sao của IMG_20261024_180445 - Đoàn Hải Yến-SBD1.jpg` | Car | 1,554.6 KB | 27.8 KB | 98.2% |
| 11 | `69 - Car/IMG_20261026_120247 - Đoàn Hải Yến-SBD1.jpg` | Car | 2,767.6 KB | 39.3 KB | 98.6% |
| 12 | `75 - Bike/IMG_20261026_131852 - Đoàn Hải Yến-SBD1.jpg` | Bike | 2,269.0 KB | 49.7 KB | 97.8% |
| 13 | `81 - Bike/IMG_20261026_145448 - Đoàn Hải Yến-SBD1.jpg` | Bike | 2,538.5 KB | 46.9 KB | 98.2% |
| 14 | `87 - Car/IMG_20261026_162040 - Đoàn Hải Yến-SBD1.jpg` | Car | 2,374.6 KB | 52.6 KB | 97.8% |
| 15 | `93 - Car/IMG_20261027_095604 - Đoàn Hải Yến-SBD1.jpg` | Car | 2,801.6 KB | 102.1 KB | 96.4% |

---

## 3. Image Normalization & WebP Compression Analysis

The normalization module (`services/normalizer.py:sanitize_and_normalize_image`) implements four essential guarantees:
1. **Zero Double-Rotation**: Evaluates EXIF Tag 274 orientation and normalizes pixel arrays via `ImageOps.exif_transpose`, stripping orientation metadata upon WebP re-encoding.
2. **Alpha Blackout Prevention**: Replaces transparent pixels with solid white backing canvas for `RGBA`, `LA`, and `P` palette modes, eliminating the black background corruption bug common in consumer mobile uploads.
3. **Draft Scaling Pre-allocation**: Uses `libjpeg` C-level draft scaling (`im.draft("RGB", (1024, 1024))`) to bound RAM allocation before decoding raw full-frame sensor buffers.
4. **Lanczos Downsampling**: Scales long edges to 1,024 px while preserving high-contrast edge fidelity necessary for OCR.

### Compression Statistics

- **Raw Payload Size**:
  - Sample Mean: **2,172.9 KB** (Median: 2,277.5 KB; Range: 105.7 KB to 4,337.2 KB)
  - Full Dataset Mean: **2,095.4 KB** (Range: 51.6 KB to 16,448.8 KB)
- **WebP Output Size (Quality 82)**:
  - Sample Mean: **49.4 KB** (Median: 47.5 KB; Range: 23.8 KB to 102.1 KB)
  - Full Dataset Mean: **43.3 KB** (Range: 14.8 KB to 151.7 KB)
- **Bandwidth Reduction**:
  - Sample Benchmark: **93.87% reduction** (p50: 97.83%)
  - Full Dataset: **97.9% reduction** across 648 files.

This >93% reduction yields huge savings in API transmission latency, egress bandwidth, and upstream token footprint when transmitting base64 images to vision LLMs.

---

## 4. Blur Detection Performance (Laplacian Variance)

Blur detection uses the second-order derivative variance of the normalized grayscale image (`cv2.Laplacian(gray, cv2.CV_64F).var()`). An empirical threshold of **$\text{variance} \ge 100.0$** distinguishes sharp, human-readable text from blurry captures.

### Distribution Analysis

```
Laplacian Variance Distribution (Full Dataset: 648 images)
0 ------- [ 100.0 (Threshold) ] --------------------------------- 2336.6
  Blurry: 102 images (15.7%)   |     Sharp: 546 images (84.3%)
  Median: 236.6                |     Mean: 407.8
```

- **Benchmark 15-Sample Subset**:
  - Mean Variance: **440.8** (Median / p50: **350.6**, p95: **1,073.9**)
  - Minimum: 107.8 (`32 - Bike/...`), Maximum: 1,947.0 (`01 - Bike/...`)
  - **100% (15/15)** exceeded the 100.0 threshold and were deemed sharp.
- **Dataset-Wide Audit (648 images)**:
  - Minimum Variance: **16.3** (severe out-of-focus phone screen capture)
  - Maximum Variance: **2,336.6** (high-contrast screen text)
  - Mean: **407.8**, Median: **236.6**
  - **Blur Rejections**: **102 images (15.7%)** fell below 100.0.

**Finding**: Pre-filtering images with Laplacian blur detection successfully weeds out ~15.7% of unusable submissions before invoking the vision LLM, preventing unnecessary inference expenses and false OCR hallucinations on unreadable text.

---

## 5. Photoshop & Digital Tampering Forensic Results

The pipeline incorporates a dual-phase tamper detection module (`detect_image_manipulation` in `services/normalizer.py`):
1. **EXIF Software Metadata Scan**: Reads EXIF Tag 305 (`Software`) and flags matches against banned manipulation packages: `photoshop`, `canva`, `gimp`, `picsart`, `lightroom`, `snapseed`, `meitu`, `photoscape`, `facetune`, `procreate`.
2. **Error Level Analysis (ELA)**: Re-saves the normalized image buffer as JPEG at 95% quality and evaluates the absolute pixel differential:
   $$\Delta_{\text{ELA}} = |I_{\text{original}} - I_{\text{JPEG95}}|$$
   A high localized maximum difference coupled with a low baseline mean difference ($\text{ELA}_{\max} > 85$ and $\text{ELA}_{\text{mean}} < 8.0$) indicates localized compression anomalies characteristic of spliced or cloned image regions.

### Forensic Audit Findings

Across all 648 images in the GSM dataset:
- **EXIF Software Tags**: **0 images** contained banned editing software tags. All images came directly from native phone camera sensors (Xiaomi 220333QAG, Apple iPhone, Samsung) or had their EXIF stripped during prior messaging platform upload.
- **ELA Splicing Anomalies**: **2 images** were detected and flagged for localized pixel compression splicing:
  1. `20 - Bike/IMG_5035.jpeg`: Flagged as `phát hiện bất thường mức nén điểm ảnh cục bộ (ELA splice)`.
  2. `43 - Car/Bản sao của IMG_5077 - Vỹ Phan Triều.jpeg`: Flagged as `phát hiện bất thường mức nén điểm ảnh cục bộ (ELA splice)`.

Both flagged images depict driver loyalty tier and performance metric screens where specific numeric text regions exhibit localized compression disparity.

### Deterministic Rule Engine Integration

When an image is flagged by forensic ELA or metadata analysis, the pipeline propagates this finding:
```python
if is_tampered_forensic and not facts.is_manipulated_or_photoshopped:
    facts.is_manipulated_or_photoshopped = True
    facts.tamper_evidence = tamper_reason
```
The deterministic evaluator (`evaluate_survey_compliance`) immediately rejects the submission with reason code **`DIGITAL_TAMPERING`**, preventing prompt-injection attacks from overriding forensic visual reality.

---

## 6. Vision AI Extraction & Latency Benchmarks

### Latency Profiles (15 Samples)

All timing measurements reflect wall-clock execution on Linux x86_64:

| Stage | Mean (ms) | Median / p50 (ms) | p95 (ms) | Min (ms) | Max (ms) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Image Normalization** | 302.0 | 316.7 | 347.8 | 174.1 | 370.1 |
| **Gemini AI Vision Extraction** | 4,723.4 | 4,367.0 | 6,391.6 | 3,764.3 | 7,595.2 |
| **End-to-End Pipeline Latency** | 5,096.9 | 4,764.2 | 6,758.3 | 4,128.5 | 8,017.2 |

- Normalization is deterministic and fast: **p50 = 316.7 ms, p95 = 347.8 ms**.
- Multimodal vision inference over Google Cloud API: **p50 = 4.37 s, p95 = 6.39 s**.

### Extraction Precision & Field Parsing

The vision client extracted structured Vietnamese facts into `ExtractedReceiptFacts`:
- **Line Items**: Correctly transcribed tabular income items including:
  - `"Doanh số"` (Gross sales)
  - `"Chiết khấu + thuế"` (Discounts & platform tax withholding, parsed as negative integer amounts, e.g., `-245881`)
  - `"Thu nhập ròng"` (Net income)
  - Periodic dates: e.g., `"07 - 13/07/2026"`, `"21 - 27/09/2026"`.
- **Anti-Spoofing Recapture Detection**: **100% of tested smartphone screen captures** were flagged with `is_screen_photo: true`.
- **Verdict Agreement**: In the paper receipt compliance evaluation mode, all 15 samples received the verdict **`SCREEN_PHOTO`** due to the rule requiring physical original documents rather than digital app screenshots.

---

## 7. Failure Modes, Discrepancies & Resolutions

During benchmarking and pipeline evaluation, four notable edge cases were identified and solved:

### 1. Gemini Thinking Tokens Exhausting Small Token Budgets
- **Symptom**: Vision requests configured with `max_tokens=300` failed with `finish_reason: length` after outputting partial JSON text (e.g. `{"vendor_name`).
- **Root Cause**: Gemini 3.5 Flash includes internal reasoning tokens within the completion token allocation. A tight limit of 300 tokens leaves insufficient headroom for both reasoning and output JSON serialization.
- **Resolution**: Increased `max_tokens` to `2048` in `services/vision_ai.py`, ensuring full completion without truncation.

### 2. Free Tier Quota Exhaustion (HTTP 429) & Multi-Model Tiering
- **Symptom**: Sequential testing of multiple images rapidly hit the 20 requests/day free tier quota on `gemini-3.5-flash` (`RESOURCE_EXHAUSTED`).
- **Resolution**: Implemented automatic fallback within `DualModelVisionClient._call_gemini` to try `gemini-3.5-flash` and immediately fall back to `gemini-3.5-flash-lite` upon encountering a 429 quota exception.

### 3. Null Values & Formatted Currency String Coercion
- **Symptom**: Survey photos lacking paper receipt headers (subtotals, VAT) returned `null` from Gemini, which caused Pydantic validation errors (`Input should be a valid integer`).
- **Resolution**: Added `@field_validator(..., mode="before")` on `ReceiptLineItem.amount` and `ExtractedReceiptFacts.raw_subtotal/raw_total` to safely coerce `None`, formatted strings (`"1.244.000đ"`), and negative values into standard integer representations.

### 4. Decompression Bomb Attack Guardrail
- **Observation**: During full-dataset scanning, one image in folder `43 - Car` had a resolution of 48,771,072 pixels (~48.8 MP), triggering PIL's `DecompressionBombWarning`.
- **Status**: The pipeline setting `Image.MAX_IMAGE_PIXELS = 35_000_000` in `services/normalizer.py` successfully flagged the oversized buffer, protecting against image-based denial-of-service memory exhaustion.

---

## 8. Summary of Results per Benchmark Image

| # | Image | Raw $\to$ WebP | Laplacian | Tamper | Screen | Extracted Total | Verdict | Total Latency |
| :-: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| 1 | `01 - Bike/...` | 105.7KB $\to$ 23.8KB | 1,947.0 | Clean | True | 0 đ | SCREEN_PHOTO | 4,180.6 ms |
| 2 | `101 - Car/...` | 2,033.7KB $\to$ 47.5KB | 357.9 | Clean | True | 0 đ | SCREEN_PHOTO | 4,989.6 ms |
| 3 | `108 - Bike/...` | 4,337.2KB $\to$ 63.4KB | 507.8 | Clean | True | 2,321,732 đ | SCREEN_PHOTO | 4,565.1 ms |
| 4 | `16 - Bike/...` | 2,256.9KB $\to$ 46.1KB | 304.0 | Clean | True | 0 đ | SCREEN_PHOTO | 4,788.4 ms |
| 5 | `24 - Bike/...` | 2,277.5KB $\to$ 49.4KB | 188.9 | Clean | True | 0 đ | SCREEN_PHOTO | 4,507.3 ms |
| 6 | `32 - Bike/...` | 1,827.2KB $\to$ 26.3KB | 107.8 | Clean | True | 0 đ | SCREEN_PHOTO | 4,511.5 ms |
| 7 | `37 - Car/...` | 2,477.9KB $\to$ 56.6KB | 191.9 | Clean | True | 0 đ | SCREEN_PHOTO | 5,194.7 ms |
| 8 | `44 - Car/...` | 167.2KB $\to$ 72.4KB | 466.4 | Clean | True | 0 đ | SCREEN_PHOTO | 6,218.8 ms |
| 9 | `56 - Car/...` | 2,804.8KB $\to$ 36.5KB | 336.6 | Clean | True | 0 đ | SCREEN_PHOTO | 4,723.4 ms |
| 10 | `62 - Car/...` | 1,554.6KB $\to$ 27.8KB | 154.7 | Clean | True | 0 đ | SCREEN_PHOTO | 4,764.2 ms |
| 11 | `69 - Car/...` | 2,767.6KB $\to$ 39.3KB | 350.6 | Clean | True | 0 đ | SCREEN_PHOTO | 4,128.5 ms |
| 12 | `75 - Bike/...` | 2,269.0KB $\to$ 49.7KB | 407.5 | Clean | True | 3,847,671 đ | SCREEN_PHOTO | 5,217.9 ms |
| 13 | `81 - Bike/...` | 2,538.5KB $\to$ 46.9KB | 399.3 | Clean | True | 4,059,000 đ | SCREEN_PHOTO | 6,017.0 ms |
| 14 | `87 - Car/...` | 2,374.6KB $\to$ 52.6KB | 191.5 | Clean | True | 9,181,500 đ | SCREEN_PHOTO | 4,629.4 ms |
| 15 | `93 - Car/...` | 2,801.6KB $\to$ 102.1KB | 699.7 | Clean | True | 5,713,000 đ | SCREEN_PHOTO | 8,017.2 ms |

---

## 9. Recommendations for Production

1. **Category-Aware Rule Sets**:
   The current compliance evaluator is tuned for paper physical inspection receipts. For vehicle fleet surveys (like GSM), a dedicated rule set should be added that distinguishes acceptable mobile app earnings screenshots from fraudulent recapture.
2. **Hybrid ELA + AI Fusion**:
   Continue combining deterministic ELA anomaly detection with LLM reasoning. ELA catches pixel resaving boundaries that LLMs cannot compute from decoded RGB tensors alone, while LLMs provide context on whether the image is an app screenshot, physical photo, or document.
3. **Bandwidth Optimization**:
   Keep the 1024 px WebP quality 82 normalization pipeline enabled across all upload ingestion points; the 93.9%–97.9% payload reduction significantly improves edge upload performance and cloud processing costs.
