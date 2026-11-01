# Adversarial Red-Team Security & Reliability Assessment Report

**Target System:** Mobile Survey Verification Pipeline (`/home/ebylxn/projects/survey-pipeline`)  
**Assessment Date:** 2026-11-01  
**Assessment Scope:** Ingress & Auth, Media Sanitization, Fleet Anti-Fraud & Screening, AI Vision & Prompt Injection, Telegram Bot & Outbox Persistence  
**Test Suite:** `tests/test_adversarial_break.py` (29 Automated Adversarial Tests — 100% Passing)

---

## Executive Summary

An adversarial red-team assessment was conducted against the survey verification pipeline. The pipeline processes mobile submissions (driver license plates, receipts, vehicle photos, revenue figures, signatures, and GPS) using FastAPI, Pillow, OpenCV, DeepSeek/Gemini multimodal models, deterministic Python compliance engines, Redis, PostgreSQL (pgvector + Transactional Outbox), and Telegram Bot gateways.

The attack campaign identified **17 specific vulnerabilities and design weaknesses** across all 5 architectural tiers. Highlights include:
1. **Critical Anti-Fraud Bypass:** Submitting an empty license plate (`detected_plate=""`) completely bypasses vehicle verification and auto-approves.
2. **Critical Financial Validation Flaw:** Negative gross incomes (e.g. `-50,000,000 VND`) and `float('nan')` pass income analysis as `APPROVED`.
3. **Remote 500 Crash on Handwritten Receipts:** Malformed currency strings (e.g. `"--"` or `"100-200"`) extracted by LLMs trigger unhandled Pydantic validation crashes.
4. **Outbox Poison Pill Starvation:** A single malformed event causes infinite transaction rollbacks, reports false progress counts, and permanently halts outbox worker throughput.
5. **Decompression Bomb Leak:** Pillow's default warning mechanism allows 36MP–70MP images to bypass the configured 35MP ceiling into memory.
6. **Dead AI Detector:** The 2D FFT diffusion detection formula sets a ratio threshold of `1.35` that is mathematically impossible to reach for normal non-negative spectra, leaving diffusion detection inoperative.

All vulnerabilities have been proven with repeatable test cases in `tests/test_adversarial_break.py`.

---

## Detailed Vulnerability Analysis

```
+---------------------------------------------------------------------------------------+
| Vulnerability Matrix                                                                  |
+-------------------+------------------------------------------+----------+-------------+
| Identifier        | Title                                    | Severity | Component   |
+-------------------+------------------------------------------+----------+-------------+
| VULN-FRAUD-01     | Empty License Plate Verification Bypass  | CRITICAL | Anti-Fraud  |
| VULN-FRAUD-06     | Negative & NaN Income Approved Bypass    | CRITICAL | Anti-Fraud  |
| VULN-AI-01        | Unhandled Currency Regex Parsing Crash   | HIGH     | AI Vision   |
| VULN-OUTBOX-01    | Outbox Poison Pill Infinite Loop & Lie   | HIGH     | Persistence |
| VULN-AUTH-01      | Webhook Replay Attack Vulnerability      | HIGH     | Ingress     |
| VULN-AUTH-03      | URL Verification Auth Bypass             | HIGH     | Ingress     |
| VULN-MEDIA-01     | 35MP-70MP Decompression Bomb Bypass     | MEDIUM   | Media       |
| VULN-MEDIA-02     | Truncated JPEG Leaks Unhandled OSError   | MEDIUM   | Media       |
| VULN-FRAUD-02     | Cyrillic Homoglyph Plate Stripping       | MEDIUM   | Anti-Fraud  |
| VULN-FRAUD-04     | dHash Stock Photo Bypass via Mirroring   | MEDIUM   | Anti-Fraud  |
| VULN-FRAUD-05     | 2D FFT Diffusion Detector Mathematical Deadlock| MEDIUM | Anti-Fraud  |
| VULN-AI-02        | Substring 'SUSPICIOUS' DoS in Notes      | MEDIUM   | AI Vision   |
| VULN-AI-04        | 32-bit Signed INT Overflow on Currency   | MEDIUM   | Persistence |
| VULN-RATE-01      | Appeal Counter TOCTOU Race Condition     | MEDIUM   | Telegram    |
| VULN-AUTH-02      | Missing Webhook Timestamp Freshness Check| LOW      | Ingress     |
| VULN-MEDIA-03     | White-on-White Text Vanishing Exploit    | LOW      | Media       |
| VULN-TG-01        | Telegram Callback Query Dropped Silently | LOW      | Telegram    |
+-------------------+------------------------------------------+----------+-------------+
```

---

### Component 1: Ingress & Authentication

#### VULN-AUTH-01: Webhook Replay Attack Vulnerability
- **Severity:** HIGH
- **Location:** `main.py` (`lark_webhook_ingress`), `services/auth_lark.py` (`verify_lark_signature`)
- **Vulnerability Mechanism:** The gateway calculates `sha256(timestamp + nonce + encrypt_key + raw_body)` and compares it against `X-Lark-Signature`. However, the system maintains **zero state or nonce tracking**.
- **Attack Vector:** An adversary intercepting a signed webhook payload can replay the exact same HTTP request hundreds of times. Each request is authenticated successfully, duplicating database entries and triggering repeated downstream tasks.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialIngressAndAuth::test_replay_attack_vulnerability`
- **Remediation:** Enforce Redis atomic nonce caching:
  ```python
  nonce_key = f"lock:lark_nonce:{x_lark_request_nonce}"
  is_new = await redis_client.set(nonce_key, "1", nx=True, ex=300)
  if not is_new:
      raise HTTPException(status_code=409, detail="Replay attack detected: nonce already used")
  ```

#### VULN-AUTH-02: Lack of Timestamp Freshness Window
- **Severity:** LOW
- **Location:** `services/auth_lark.py`
- **Vulnerability Mechanism:** `verify_lark_signature` concatenates `timestamp` as raw bytes without parsing or validating against the system clock.
- **Attack Vector:** A signature generated years in the past (e.g. timestamp from 1973) or far in the future remains permanently valid.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialIngressAndAuth::test_expired_timestamp_replay`
- **Remediation:** Enforce a $\pm 300$-second drift limit:
  ```python
  try:
      ts_int = int(timestamp)
      if abs(time.time() - ts_int) > 300:
          return False
  except (ValueError, TypeError):
      return False
  ```

#### VULN-AUTH-03: URL Verification Challenge Authentication Bypass
- **Severity:** HIGH
- **Location:** `main.py` lines 84–90
- **Vulnerability Mechanism:** `handle_lark_challenge_handshake` is invoked **before** signature verification. If the JSON payload contains `{"type": "url_verification", "challenge": "..."}`, the server echoes back the challenge immediately without verifying `X-Lark-Signature`.
- **Attack Vector:** Any unauthenticated caller on the public Internet can trigger the challenge responder. Furthermore, unencrypted challenge payloads are accepted even when `settings.LARK_ENCRYPT_KEY` is configured.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialIngressAndAuth::test_url_verification_challenge_bypass`
- **Remediation:** If `settings.LARK_ENCRYPT_KEY` is configured, reject unencrypted challenge payloads and mandate valid platform signatures.

---

### Component 2: Media Sanitization & Image Processing

#### VULN-MEDIA-01: Decompression Bomb Warning vs Error Bypass (35MP – 70MP)
- **Severity:** MEDIUM
- **Location:** `services/normalizer.py` line 8 (`Image.MAX_IMAGE_PIXELS = 35_000_000`)
- **Vulnerability Mechanism:** Pillow's decompression bomb protection distinguishes between warnings and errors. Sizes between `MAX_IMAGE_PIXELS` (35MP) and `2 * MAX_IMAGE_PIXELS` (70MP) emit `Image.DecompressionBombWarning`, which does **not** abort execution by default.
- **Attack Vector:** Uploading a 36MP image (e.g. 6000x6000) causes full decompression into worker memory (~108MB uncompressed raster RAM), bypassing the 35MP restriction.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialMediaSanitization::test_decompression_bomb_36mp_warning_bypass`
- **Remediation:** Elevate Pillow's warning to an exception or validate pixel count upon opening:
  ```python
  import warnings
  warnings.simplefilter("error", Image.DecompressionBombWarning)
  ```

#### VULN-MEDIA-02: Unhandled OSError on Truncated JPEG Streams
- **Severity:** MEDIUM
- **Location:** `services/normalizer.py` lines 41–48
- **Vulnerability Mechanism:** Pillow uses lazy evaluation. If an image is smaller than `max_short_edge * 2`, `im.load()` is not called inside the initial `try...except` block. When `im.save()` is subsequently called on line 87, Pillow attempts to read the truncated JPEG blocks and raises `OSError: image file is truncated`, which escapes uncaught and crashes the worker.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialMediaSanitization::test_truncated_jpeg_leaks_unhandled_oserror`
- **Remediation:** Force image decoding inside the initial `try` block:
  ```python
  try:
      im = Image.open(stream)
      im.load()  # Force raster evaluation within safe try block
  except Exception as e:
      raise ValueError(f"malformed or unreadable image stream: {e}")
  ```

#### VULN-MEDIA-03: White-on-White Text Vanishing Exploit on Alpha Channels
- **Severity:** LOW
- **Location:** `services/normalizer.py` lines 60–64
- **Vulnerability Mechanism:** All transparent images (`RGBA`, `LA`, `P`) are composited over a `#FFFFFF` white canvas.
- **Attack Vector:** If a survey or receipt image has white text or light stamps on a transparent background, compositing against white merges the foreground and background into pure `#FFFFFF`, permanently erasing text and graphics.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialMediaSanitization::test_rgba_alpha_channel_exploits`

---

### Component 3: Anti-Fraud, Fleet Consistency & Screening

#### VULN-FRAUD-01: Empty License Plate Verification Bypass (CRITICAL)
- **Severity:** CRITICAL
- **Location:** `services/vehicle_tracker.py` line 99
- **Vulnerability Mechanism:**
  ```python
  # check license plate mismatch
  if norm_detected and norm_reg and norm_detected != norm_reg:
      return VehicleMatchResult(is_match=False, status_code="VEHICLE_UNREGISTERED", ...)
  return VehicleMatchResult(is_match=True, status_code="APPROVED", ...)
  ```
  If `detected_plate` is None or empty string `""` (e.g. unreadable photo, obscured plate, or stripped field), `norm_detected` evaluates to `""`. The mismatch check is **skipped**, and the method returns `APPROVED`!
- **Attack Vector:** Drivers can submit surveys for unregistered or unauthorized vehicles simply by obscuring the license plate.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialAntiFraudAndScreening::test_vehicle_plate_empty_string_bypass`
- **Remediation:** Require a detected plate whenever a registered plate exists:
  ```python
  if norm_reg and not norm_detected:
      return VehicleMatchResult(
          is_match=False,
          is_first_registration=False,
          status_code="PLATE_NOT_DETECTED",
          flag_reason="Không tìm thấy hoặc không đọc được biển số xe trên ảnh.",
          registered_plate=reg_plate,
      )
  ```

#### VULN-FRAUD-02: Cyrillic Homoglyph Character Stripping
- **Severity:** MEDIUM
- **Location:** `services/vehicle_tracker.py` (`normalize_license_plate`)
- **Vulnerability Mechanism:** Regex `re.sub(r"[^A-Z0-9]", "", plate.upper().strip())` strips non-ASCII characters. Cyrillic lookalikes (`В` U+0412, `А` U+0410, `С` U+0421) are silently deleted.
- **Attack Vector:** `'29В-12345'` (with Cyrillic VE) normalizes to `'2912345'` instead of `'29B12345'`, causing plate mismatch or registry corruption.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialAntiFraudAndScreening::test_vehicle_plate_cyrillic_homoglyph_trick`
- **Remediation:** Transliterate known Cyrillic/Greek homoglyphs before regex stripping.

#### VULN-FRAUD-04: dHash Stock Photo Bypass via Horizontal Mirroring & Inversion
- **Severity:** MEDIUM
- **Location:** `services/online_and_ai_detector.py` (`compute_dhash_int`)
- **Vulnerability Mechanism:** Difference hash (dHash) evaluates horizontal gradients `arr[:, 1:] > arr[:, :-1]`.
- **Attack Vector:** Horizontally flipping an asymmetric stock photo (`ImageOps.mirror`) flips gradient bits, producing a Hamming distance of **64** (maximum difference), completely bypassing the threshold ($\le 6$). Inverting colors or rotating 20 degrees also bypasses detection.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialAntiFraudAndScreening::test_dhash_stock_bypass_via_horizontal_mirror_and_inversion`
- **Remediation:** Calculate dHash on both the original and horizontally flipped image:
  ```python
  target_hash = compute_dhash_int(pil_img)
  target_hash_flip = compute_dhash_int(ImageOps.mirror(pil_img))
  # Compare min(dist, dist_flip) <= threshold
  ```

#### VULN-FRAUD-05: 2D FFT Diffusion Detector Mathematical Deadlock
- **Severity:** MEDIUM
- **Location:** `services/online_and_ai_detector.py` lines 44–58
- **Vulnerability Mechanism:** The FFT detector computes:
  $$\text{ratio} = \frac{\text{high\_freq\_energy}}{\text{total\_energy}}$$
  The high-frequency mask covers **95.7%** of the 256x256 image area. For non-negative power spectra, $\text{ratio}$ can never exceed $\frac{1}{0.957} \approx 1.045$.
- **Impact:** The configured threshold of **1.35 is mathematically impossible to reach**, rendering the FFT AI detector dead code. It will never flag any image as synthetic.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialAntiFraudAndScreening::test_fft_diffusion_detection_flaw_and_blur_bypass`
- **Remediation:** Compute high-frequency to low-frequency ratio instead of total:
  ```python
  low_freq_energy = np.mean(magnitude_spectrum[~mask])
  ratio = high_freq_energy / (low_freq_energy + 1e-8)
  # Recalibrate threshold empirically (e.g. ratio > 0.45)
  ```

#### VULN-FRAUD-06: Negative and NaN Gross Income Validation Bypass (CRITICAL)
- **Severity:** CRITICAL
- **Location:** `services/income_sanitizer.py` lines 53–76
- **Vulnerability Mechanism:** The function only validates upper bounds: `if gross_income > hard_limit` and `if gross_income > soft_max`.
  1. Negative income (e.g. `-50,000,000 VND`) is $\le \text{limits}$, so it returns `is_valid: True, status_code: "APPROVED"`.
  2. `float('nan')` in Python returns `False` for both `nan > hard_limit` and `nan > soft_max`, returning `APPROVED`.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialAntiFraudAndScreening::test_income_extreme_negative_and_nan_bypass`
- **Remediation:** Add strict lower bound and NaN validation:
  ```python
  if gross_income is None or math.isnan(gross_income) or gross_income <= 0:
      return IncomeAnalysisResult(
          is_valid=False,
          status_code="INVALID_INCOME_VALUE",
          flag_reason="Thu nhập khai báo không hợp lệ (phải là số dương lớn hơn 0).",
          suggested_action="AUTO_REJECT",
      )
  ```

---

### Component 4: AI Vision & Prompt Injection

#### VULN-AI-01: Unhandled ValueError in Currency Parsing (HTTP 500 Denial of Service)
- **Severity:** HIGH
- **Location:** `services/vision_ai.py` (`ReceiptLineItem.parse_amount`, `ExtractedReceiptFacts.parse_int_or_zero`)
- **Vulnerability Mechanism:**
  ```python
  clean = re.sub(r"[^0-9\-]", "", v)
  return int(clean) if clean and clean != "-" else 0
  ```
  If an adversary writes a dash shorthand (e.g. `"--"`, `"---"`) or a range (`"100-200"`), `clean` is not equal to `"-"`. Calling `int("--")` raises an unhandled `ValueError`, crashing Pydantic deserialization and returning HTTP 500.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialAIVisionAndPromptInjection::test_schema_boundary_malformed_currency_crashes`
- **Remediation:** Use clean positive integer regex extraction:
  ```python
  digits = re.sub(r"[^0-9]", "", v)
  return int(digits) if digits else 0
  ```

#### VULN-AI-02: DoS via Substring 'SUSPICIOUS' in Notes
- **Severity:** MEDIUM
- **Location:** `services/evaluator.py` line 46
- **Vulnerability Mechanism:**
  `if facts.notes and "SUSPICIOUS" in facts.notes.upper(): return REJECTED`
- **Attack Vector:** If a store name contains "Suspicious" or if the driver notes state "Verified: not suspicious", the presence of the substring triggers an immediate rejection under `SUSPICIOUS_INCOME_ANOMALY`.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialAIVisionAndPromptInjection::test_prompt_injection_dos_via_suspicious_keyword`
- **Remediation:** Use discrete boolean flags (e.g. `is_anomalous: bool`), not substring searches on unstructured text.

#### VULN-AI-04: 32-Bit Signed Integer Overflow on Currency Fields
- **Severity:** MEDIUM
- **Location:** `services/vision_ai.py`
- **Vulnerability Mechanism:** Python integers have arbitrary precision. An LLM extracting `raw_total = "999999999999"` validates in Python but crashes PostgreSQL with `integer out of range` on columns typed `INT` (signed 32-bit: $-2^{31}$ to $2^{31}-1$).
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialAIVisionAndPromptInjection::test_currency_overflow_and_vietnamese_dot_notation`
- **Remediation:** Enforce maximum integer validation bounds in Pydantic: `Field(ge=0, le=2_147_483_647)`.

---

### Component 5: Telegram & Transactional Persistence

#### VULN-OUTBOX-01: Outbox Poison Pill Infinite Loop & False Return Count
- **Severity:** HIGH
- **Location:** `workers/outbox_worker.py` lines 12–87
- **Vulnerability Mechanism:**
  1. If event #2 in a batch of 50 fails (e.g. DB constraint violation), the entire `conn.transaction()` rolls back.
  2. **Bug:** `processed_count` is incremented in memory before commit. When the exception occurs, the function returns `processed_count = 1` even though 0 events were committed!
  3. **Poison Pill:** `retry_count` is never updated, and the status remains `PENDING`. Every subsequent poll picks up the exact same failed event, causing an infinite retry loop that starves the queue.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialTelegramAndRateLimits::test_outbox_worker_poison_pill_and_false_return_count`
- **Remediation:** Process events with savepoints or dead-letter queues:
  ```python
  except Exception as e:
      logger.error(f"failed event {ev['id']}: {e}")
      await cur.execute(
          "UPDATE outbox_events SET retry_count = retry_count + 1, status = CASE WHEN retry_count >= 5 THEN 'FAILED' ELSE 'PENDING' END WHERE id = %s;",
          (ev["id"],)
      )
  ```

#### VULN-RATE-01: Appeal Counter TOCTOU Race Condition
- **Severity:** MEDIUM
- **Location:** Redis appeal rate limiting
- **Vulnerability Mechanism:** A non-atomic read-then-increment pattern (`GET` then `INCR`) allows concurrent requests from the same driver within milliseconds to all pass, breaking the 3-per-8-hour quota.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialTelegramAndRateLimits::test_appeal_counter_race_condition_simulation`
- **Remediation:** Execute an atomic Redis Lua script:
  ```lua
  local current = redis.call('INCR', KEYS[1])
  if current == 1 then
      redis.call('EXPIRE', KEYS[1], ARGV[1])
  end
  if current > tonumber(ARGV[2]) then
      return 0
  end
  return 1
  ```

#### VULN-TG-01: Telegram Callback Queries Dropped Silently
- **Severity:** LOW
- **Location:** `main.py` lines 180–192
- **Vulnerability Mechanism:** `/webhook/telegram` inspects only `body.get('message', {})`. When drivers click inline buttons (`appeal:ID`), Telegram delivers `callback_query`, which is ignored. Drivers receive no feedback.
- **Test Evidence:** `tests/test_adversarial_break.py::TestAdversarialTelegramAndRateLimits::test_telegram_callback_query_unhandled_drop`

---

## Hardening Recommendations & Priority Roadmap

| Priority | Action Item | Affected File | Fix Complexity |
|---|---|---|---|
| **P0** | Reject empty detected plates (`norm_reg and not norm_detected`) | `services/vehicle_tracker.py` | 3 lines |
| **P0** | Reject non-positive (`<= 0`) and NaN incomes | `services/income_sanitizer.py` | 4 lines |
| **P0** | Sanitize currency parsing regex to strip hyphens cleanly (`[^0-9]`) | `services/vision_ai.py` | 2 lines |
| **P1** | Add dead-letter queue and retry counter increment on outbox errors | `workers/outbox_worker.py` | 12 lines |
| **P1** | Add Redis atomic nonce caching to prevent webhook replay | `main.py` | 8 lines |
| **P1** | Check `abs(time.time() - ts) <= 300` on Lark signatures | `services/auth_lark.py` | 6 lines |
| **P2** | Fix FFT ratio calculation to compare high against low frequencies | `services/online_and_ai_detector.py` | 5 lines |
| **P2** | Evaluate dHash against horizontal mirror to prevent flipping bypass | `services/online_and_ai_detector.py` | 4 lines |
| **P2** | Call `im.load()` inside safe `try` block to catch truncated JPEGs | `services/normalizer.py` | 1 line |
| **P3** | Handle `callback_query` in Telegram webhook with atomic Lua limiter | `main.py` | 15 lines |

---

## Verification Summary

All 29 adversarial attack scenarios have been integrated into `tests/test_adversarial_break.py`. Executing:

```bash
PYTHONPATH=. uv run pytest tests/test_adversarial_break.py -v
```

Yields **29 passed in 7.03s** with zero regressions on the existing test suite (67 passed total). All tests use hermetic in-memory mocks (`AsyncMock`, `MagicMock`) for database pools and external network sockets, running reliably without hanging.
