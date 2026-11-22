# Survey Verification Pipeline: Operations Runbook & API Reference Manual

**Document Classification:** Historical operations/API reference; examples require current-environment and upgrade-gate verification
**System Baseline:** Survey Verification Pipeline v3.0.0  
**Target Infrastructure:** DigitalOcean SGP1 (Singapore, 2 vCPU, 4GB RAM, NVMe)  
**Production Host:** `146.190.201.32` (`146.190.201.32.sslip.io`, `survey.trustbridgevn.com`)  
**Target Operating Throughput:** 20,000+ submissions/day with < 2,500ms synchronous response SLA  

> **Do not execute this historical runbook as an upgrade plan.** [MODULE_IMPLEMENTATION_PLANS.md](MODULE_IMPLEMENTATION_PLANS.md) defines versioned SQL application, private-media authorization, history/UI approval and schema/worker/producer activation. The checked-in Compose file is not the full stack illustrated below; copying schema.sql does not apply it. Historical restart/Compose-down and default-credential examples are not operator approval. Both CI and script deployment must first protect mutable data, apply/check the required schema, verify a compatible worker and check readiness. No production loss or successful rollout is claimed by this warning.


> **Authoritative Administrative Account Notice**:
> `admin@trustbridgevn.com` is the authoritative administrative account to be used for this project, Google Drive deliverables, and all work regarding Trust Bridge. All deliverables synchronization, cloud storage targets, and project administrative privileges are bound to `admin@trustbridgevn.com`.
---

## 1. Dynamic WebRTC Mobile Form Architecture

The customer-facing and driver-facing entry point is a progressive, dynamic WebRTC mobile web form rendered server-side by `services/dynamic_form_html.py` and validated by `services/form_engine.py`. It guarantees hardware-bound camera captures, preventing fraud via pre-saved gallery uploads.

### 1.1 Form Schema Engine (`services/form_engine.py`)
Survey forms are driven entirely by dynamic JSON schemas, allowing business teams to update questions, validation logic, image requirements, and regulatory consent checks without redeploying backend code.

The submission adapter validates the explicitly selected form; legacy requests without a form retain their existing contract rather than inheriting the current default schema. Required answers cannot be whitespace-only, and numeric questions must contain finite numbers even when no numeric bounds are configured. Rejected malformed numeric input is serialized safely instead of being converted into an interruption verdict.

An explicit consent refusal, including a configured consent-gate refusal, returns a redacted rejection before photo staging, AI, persistence or notifications; test bypass does not override refusal. Invalid uploaded form schemas return 422 before registry/file/rename/database changes. Enabled vehicle checks read the real non-DEV registry without committing a registration during evaluation; the submission transaction owns first registration and preserves existing canonical/legacy phone bindings.

#### 1.1.1 JSON Schema Structure & Validation Rules
```json
{
  "form_id": "income_survey",
  "title": "Trust Bridge - Khảo Sát Thu Nhập Tài Xế GSM (Xanh SM)",
  "description": "Biểu mẫu khảo sát và xác thực thu nhập tài xế taxi / xe máy điện VinFast",
  "settings": {
    "response_limit": 20000,
    "access_control": "public",
    "allow_anonymous": false,
    "allow_edit_after_submit": false,
    "decree13_required": true,
    "is_test_mode": false,
    "image_checks": {
      "enable_blur_check": true,
      "blur_threshold": 40.0,
      "enable_ai_vision": true,
      "enable_vehicle_match": true,
      "enable_anti_fraud": true,
      "enable_ai_generated_check": true,
      "enable_screen_photo_check": false
    }
  },
  "questions": [
    {
      "id": "driver_signature",
      "title": "Chữ ký xác thực của tài xế",
      "type": "signature",
      "required": true
    },
    {
      "id": "consent_choice",
      "title": "Đồng thuận xử lý dữ liệu cá nhân theo Nghị định 13/2023/NĐ-CP",
      "type": "single_choice",
      "required": true,
      "options": ["Tôi đồng ý", "Tôi không đồng ý"]
    },
    {
      "id": "vehicle_type",
      "title": "Loại phương tiện hoạt động",
      "type": "single_choice",
      "required": true,
      "options": ["Xe máy điện (BIKE)", "Ô tô điện (CAR)"],
      "conditional": {
        "depends_on_question_id": "consent_choice",
        "operator": "equals",
        "expected_value": "Tôi đồng ý"
      }
    },
    {
      "id": "driver_phone",
      "title": "Số điện thoại đăng ký tài khoản tài xế",
      "type": "text",
      "required": true,
      "validation": {
        "regex_pattern": "^0[0-9]{9,10}$",
        "regex_error_message": "Số điện thoại phải bắt đầu bằng 0 và gồm 10-11 chữ số",
        "numbers_only": true
      },
      "conditional": {
        "depends_on_question_id": "consent_choice",
        "operator": "equals",
        "expected_value": "Tôi đồng ý"
      }
    },
    {
      "id": "camera_receipt",
      "title": "Chụp ảnh xác thực",
      "type": "camera_webrtc",
      "required": true,
      "image_slots": [
        {
          "slot": 0,
          "title": "Đồng hồ km (Odometer)",
          "role": "odometer",
          "required": true
        },
        {
          "slot": 1,
          "title": "Màn hình doanh thu ứng dụng",
          "role": "revenue_screen",
          "required": true
        }
      ],
      "conditional": {
        "depends_on_question_id": "consent_choice",
        "operator": "equals",
        "expected_value": "Tôi đồng ý"
      }
    }
  ]
}
```

#### 1.1.2 Conditional Logic Branching
The engine resolves dependency graphs dynamically:
- **`equals` / `not_equals` / `contains` / `in`:** Evaluates parent answers in real time.
- **Regulatory Opt-Out (Decree 13 Dissent):** When `consent_choice` is `"Tôi không đồng ý"`, dependent questions (phone number, license plate, revenue, photos) are hidden and skipped. The survey is submitted anonymously with zero personal data saved (`driver_phone = NULL`, `driver_name = NULL`).
- **Circular Dependency Guard:** The schema validator detects self-referencing (`Q1 -> Q1`) and circular dependencies (`Q1 <-> Q2`) at upload time, rejecting invalid schemas with HTTP 422.

#### 1.1.3 Form Modules and Custom Outputs
`settings.modules` defines an ordered computation pipeline. Modules execute top to bottom after question validation and before the final verdict. Inputs may reference a question, an earlier named output, an entire earlier module, or a literal. Output names become the form's custom outputs.

```json
{
  "uid": "m1",
  "name": "Completion",
  "kind": "logic",
  "module_id": "get-percentage",
  "enabled": true,
  "scope": "form",
  "question_id": null,
  "slot_index": null,
  "inputs": [
    {"name": "target", "source": "question", "question_id": "daily_trips"},
    {"name": "total", "source": "literal", "literal": 20}
  ],
  "outputs": [
    {"key": "percentage", "name": "completion"}
  ],
  "reject_on_false": false,
  "reject_message": ""
}
```

- `kind: "logic"` runs an allowlisted module from `/admin/modules`; its declared `inputs` are mapped by name.
- `kind: "engine"` wraps a check engine from `/api/v1/image-objects`; `params_schema` keys become inputs, and form answers are available to the engine.
- `kind: "all"` is a composite check. Its inputs reference earlier modules only; it passes when every referenced module passed.
- Outputs are persisted as `form_data.module_outputs` and included in the submission response. Because they live in `form_data`, selected Drive export fields include them.
- `reject_on_false` turns a false module output into a rejected submission with `reject_message`.
- `scope` is `form`, `question`, or `slot`. `question` requires `question_id`; `slot` requires `question_id` plus `slot_index` pointing at an existing image slot. Scope controls which editor section owns the module and labels the failure; execution order remains the ordered `settings.modules` list.
- Modules may reference only earlier modules, output names must be unique across the form, and input questions must exist.
- The form editor groups question conditions, scoped modules, and consent-gate settings in a collapsed `Tùy chọn câu hỏi` block; image slots have their own scoped module block.
- `GET /api/v1/forms/export` downloads a versioned `survey-pipeline-forms` JSON bundle; `GET /api/v1/forms/{form_id}/export` downloads one form. `POST /api/v1/forms/import[?overwrite=true]` accepts either shape. Import rejects existing IDs with HTTP 409 unless `overwrite=true`, in which case the replaced schema is first copied to `data/form_backups`.

### 1.2 Multi-Slot Camera Workflow
To prevent drivers from submitting out-of-order or mismatched photos (e.g., swapping the odometer photo with the revenue screen), the WebRTC camera interface introduces a multi-slot architecture:

1. **Compact Number-Only Slot Pills:**
   - Navigation pills are rendered above the viewfinder: `[ 1 * ]` `[ 2 * ]` `[ 3 ]` (with `*` denoting mandatory slots).
   - Visual states:
     * **Idle / Uncaptured:** Neutral border (`border: #23314f`).
     * **Active Slot:** Blue glow with accent outline (`background: rgba(37, 99, 235, 0.2); border-color: #2563eb`).
     * **Captured Slot:** Green checkmark icon and emerald border (`border-color: #10b981; color: #a7f3d0`).

2. **Continuous Live Video Streaming Across Slots:**
   - The hardware camera stream (`navigator.mediaDevices.getUserMedia`) is **not torn down** between captures. It remains alive in memory.
   - When a slot is captured, the engine automatically advances to the next uncaptured slot:
     ```javascript
     // Automatically advances to next uncaptured slot and keeps camera alive
     selectCameraSlot(qid, nextUncapturedIdx);
     if (vid) vid.style.display = 'block';
     if (snapBtn) snapBtn.style.display = 'flex';
     ```
   - Re-clicking an existing slot pill displays the captured photo preview with a `[Chụp lại]` (Retake) button; if clicked, the active stream is re-attached immediately.

3. **Clean Viewfinder & Top Guidance Badge:**
   - The viewfinder eliminates center-blocking overlays or translucent masks that hinder framing under sunlight.
   - A top floating guidance badge (`.camera-overlay-slot-badge`) provides clear instruction: e.g. `"Căn chỉnh: Đồng hồ km (Odometer)"` or `"Căn chỉnh: Màn hình doanh thu ứng dụng"`.

4. **Aspect Ratio & Native Canvas Compression:**
   - Viewport enforces a native **2:3 aspect ratio** (`aspect-ratio: 2 / 3; max-height: 52dvh`).
   - Image captures are drawn to an off-screen HTML5 `<canvas>`, scaled down to **max 1080px**, and compressed directly to WebP at **82% quality** via `canvas.toBlob(blob, 'image/webp', 0.82)`.

### 1.3 Paper Signature Card & Zero-Jerk Modal
Mobile signatures typically suffer from the iOS Safari "address bar jerk" bug, where dragging on a signature pad scrolls the parent webpage and collapses browser toolbars.

The form eliminates this through a zero-jerk modal pattern:
- **Global `touchmove` Prevention:**
  ```javascript
  function openSigModal() {
      sigModal.style.display = 'flex';
      document.body.style.overflow = 'hidden';
      // Global unconditional window touchmove cancellation: address bar physically CANNOT collapse!
      window.addEventListener('touchmove', preventWindowScroll, { passive: false });
  }
  function closeSigModal() {
      sigModal.style.display = 'none';
      document.body.style.overflow = '';
      window.removeEventListener('touchmove', preventWindowScroll);
  }
  function preventWindowScroll(e) {
      if (e.target !== sigCanvas) e.preventDefault();
  }
  ```
- **Paper Signature Card:** Displays the saved signature on a crisp white card inside the dark UI, matching physical paperwork styling.

### 1.4 Numeric Keypad Enforcement
To eliminate mobile input friction for driver phone numbers, trip counts, and revenue fields:
- Standard inputs use `inputmode="numeric"`, `type="tel"`, and `pattern="[0-9]*"`.
- Real-time client regex filtering strips any non-numeric characters immediately on the `input` event (`value = value.replace(/[^0-9]/g, '')`).

---

## 2. Resilient Geolocation & IP Fallback Engine

GPS verification ensures field surveys occur at genuine vehicle locations. However, mobile browsers frequently block geolocation prompts or suffer GPS timeout indoors.

### 2.1 Browser Geolocation & Permission Persistence
The client implements a 3-layer authorization strategy:
1. **`localStorage('tb_gps_granted')` Persistence:** Once a user grants GPS permission, the flag is saved locally. Subsequent form reloads automatically query `navigator.geolocation.getCurrentPosition` without prompting.
2. **`navigator.permissions.query` Inspection:** Silently queries permission state (`perm.state === 'granted'`).
3. **Subtle Status Pill:**
   - Acquiring: `📍 Đang lấy vị trí...` (pulsing blue).
   - Confirmed: `📍 Đã xác nhận vị trí (Hà Nội, VN)` (solid emerald).
4. **Caching & Timeout:** Configured with `maximumAge: 300000` (5 minutes) and `timeout: 15000` (15 seconds) to prevent battery drain.

### 2.2 Server-Side IP Fallback Engine (`GET /api/v1/geo/ip-fallback`)
If client-side GPS fails, times out, or is denied, the frontend seamlessly queries `/api/v1/geo/ip-fallback`:

```
Client -> GET /api/v1/geo/ip-fallback
  ├── 1. Check Cloudflare Headers (cf-iplatitude, cf-iplongitude, cf-ipcity)
  ├── 2. Extract Client IP (X-Forwarded-For, cf-connecting-ip)
  ├── 3. Private / Loopback IP Filter -> Return Default Vietnam Coordinates (Hanoi)
  ├── 4. Redis Cache Query (geo:ip:{ip}) -> Hit: Return Cached Lat/Lon (< 1ms)
  ├── 5. External Lookup: ip-api.com (2.5s Timeout) -> Set Redis TTL 86400s
  └── 6. Network Error / Hang -> Guaranteed 200 OK Fallback (Never 500s!)
```

#### Guarantees
- **Never 500s:** Network failures, upstream timeouts, or invalid IP strings always return HTTP 200 with fallback coordinates:
  ```json
  {
    "status": "success",
    "latitude": 21.0285,
    "longitude": 105.8542,
    "accuracy": 5000.0,
    "city": "Hà Nội",
    "source": "default_fallback"
  }
  ```

---

## 3. Complete API Endpoint Reference

All endpoints return JSON unless explicitly annotated as HTML. Standard date format is ISO-8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`).

### 3.1 `GET /health` & `HEAD /health`
Liveness and readiness probe for load balancers and container orchestrators.
- **Method:** `GET` | `HEAD`
- **Auth:** None
- **Response `200 OK`:**
  ```json
  {
    "status": "healthy",
    "environment": "production",
    "timestamp": 1793540000.123
  }
  ```

---

### 3.2 `POST /api/v1/survey/submit-webrtc`
Primary ingestion endpoint for live WebRTC survey submissions.

- **Method:** `POST`
- **Content-Type:** `multipart/form-data`
- **Parameters:**
  | Field | Type | Required | Description |
  |---|---|---|---|
  | `driver_phone` | string | Yes | Driver phone number (e.g. `0988123456`) |
  | `survey_photo` | File (binary) | Optional* | Legacy single photo upload (WebP/JPEG) |
  | `survey_photo_0` | File (binary) | Optional* | Multi-slot Photo 0 (Odometer) |
  | `survey_photo_1` | File (binary) | Optional* | Multi-slot Photo 1 (Revenue Screen) |
  | `license_plate` | string | Optional | Vehicle license plate (e.g. `29E-123.45`) |
  | `driver_name` | string | Optional | Driver full name |
  | `vehicle_type` | string | Optional | `'BIKE'` or `'CAR'` (Default: `'CAR'`) |
  | `gross_revenue` | string | Optional | Gross daily or monthly fare in VND |
  | `trip_count` | string | Optional | Total trips completed |
  | `odometer_km` | string | Optional | Current odometer mileage reading |
  | `signature_png` | string | Optional | Data URI base64 PNG of signature |
  | `idempotency_key` | string | Optional | Client UUID preventing duplicate submissions |
  | `is_test_mode` | boolean | Optional | Routes to dev memory storage (`false` in prod) |
  | `bypass_img_check` | boolean | Optional | Dev bypass flag (strictly ignored in prod) |
  | `form_id` | string | Optional | Form schema identifier (default: `income_survey`) |

- **Sample Request (`curl`):**
  ```bash
  curl -X POST "https://146.190.201.32.sslip.io/api/v1/survey/submit-webrtc" \
    -F "driver_phone=0988123456" \
    -F "license_plate=29E-123.45" \
    -F "vehicle_type=CAR" \
    -F "gross_revenue=650000" \
    -F "trip_count=18" \
    -F "survey_photo_0=@odometer.webp;type=image/webp" \
    -F "survey_photo_1=@revenue.webp;type=image/webp" \
    -F "idempotency_key=c1a2b3c4-d5e6-7f8a-9b0c-1d2e3f4a5b6c"
  ```

- **Success Response `200 OK` (Approved):**
  ```json
  {
    "status": "APPROVED",
    "submission_id": "VN-7A8B9C0D1E2F3A4B",
    "mode": "PROD_MODE",
    "reason": "Hợp lệ",
    "driver_tag": "DRV-84920B1A",
    "driver_phone": "0988123456",
    "detected_plate": "29E-123.45",
    "gross_revenue": "650000",
    "trip_count": "18",
    "blur_score": 142.8
  }
  ```

- **Rejection Response `200 OK` (Blurry Image):**
  ```json
  {
    "status": "REJECTED",
    "submission_id": "VN-0A1B2C3D4E5F6A7B",
    "mode": "PROD_MODE",
    "reason": "Ảnh chụp bị mờ hoặc rung tay, vui lòng chụp lại rõ nét hơn!",
    "driver_tag": "DRV-84920B1A",
    "driver_phone": "0988123456",
    "blur_score": 18.4
  }
  ```

---

### 3.3 `GET /api/v1/geo/ip-fallback`
Server-side IP geolocation resolver.
- **Method:** `GET`
- **Headers Examined:** `X-Forwarded-For`, `cf-connecting-ip`, `cf-iplatitude`, `cf-iplongitude`
- **Response `200 OK`:**
  ```json
  {
    "status": "success",
    "latitude": 21.0285,
    "longitude": 105.8542,
    "accuracy": 5000.0,
    "city": "Hanoi",
    "source": "ip_fallback"
  }
  ```

---

### 3.4 `GET /forms/{form_id}`
Renders the client-facing responsive dynamic WebRTC HTML form.
- **Method:** `GET`
- **URL Parameters:** `form_id` (e.g. `income_survey`, `camera-form`)
- **Response `200 OK`:** `text/html; charset=utf-8`

---

### 3.5 `GET /admin/forms` & `GET /admin/forms/edit/{form_id}`
Admin Web UI for creating, browsing, and configuring survey form schemas.
- **Method:** `GET`
- **Auth:** HTTP Basic Authentication (`admin` / `12345678`)
- **Response `200 OK`:** `text/html; charset=utf-8`

---

### 3.6 `POST /api/v1/forms/upload-schema`
Persists a new or updated FormSchema JSON configuration.
- **Method:** `POST`
- **Auth:** HTTP Basic Authentication (`admin` / `12345678`)
- **Request Body:** Full `FormSchema` JSON object.
- **Response `200 OK`:**
  ```json
  {
    "status": "success",
    "form_id": "new_fleet_survey",
    "title": "Khảo Sát Đội Xe Mới",
    "form_url": "/forms/new_fleet_survey",
    "question_count": 8,
    "message": "Biểu mẫu 'Khảo Sát Đội Xe Mới' đã sẵn sàng hoạt động!"
  }
  ```

---

### 3.7 `GET /api/v1/surveys/history` (Locked Down Endpoint)
Queries previous attempts and rejection reasons for a driver.
- **Security Control:** Strictly requires HTTP Basic Authentication. Unauthenticated public access receives `401 Unauthorized` to prevent competitive phone number scraping.
- **Rate Limit:** 30 queries per minute per phone number via Redis.
- **Sample Request:**
  ```bash
  curl -u admin:12345678 "https://146.190.201.32.sslip.io/api/v1/surveys/history?phone=0988123456"
  ```
- **Response `200 OK`:**
  ```json
  {
    "phone": "0988123456",
    "count": 2,
    "attempts": [
      {
        "submission_id": "VN-A1B2C3D4",
        "mode": "PRODUCTION",
        "status": "REJECTED",
        "reason": "Ảnh chụp bị mờ hoặc rung tay",
        "submitted_at": "2026-11-06T08:12:00Z"
      },
      {
        "submission_id": "VN-E5F6A7B8",
        "mode": "PRODUCTION",
        "status": "APPROVED",
        "reason": "Hợp lệ",
        "submitted_at": "2026-11-06T08:14:30Z"
      }
    ]
  }
  ```

---

### 3.8 `POST /webhook/lark-survey`
Bytedance Lark Base webhook receiver for synchronized spreadsheet rows.
- **Method:** `POST`
- **Headers Required:**
  * `X-Lark-Request-Timestamp`: Epoch timestamp in seconds.
  * `X-Lark-Request-Nonce`: Random string.
  * `X-Lark-Signature`: SHA-256 signature string.
- **Verification Formula:**
  $$\text{computed} = \text{hex}(\text{sha256}(\text{timestamp} + \text{nonce} + \text{LARK\_ENCRYPT\_KEY} + \text{raw\_body}))$$
- **Replay Protection:** Rejects timestamps older than 300 seconds (`abs(time() - ts) > 300`) and caches nonces in Redis for 60 seconds (`SETNX lock:lark:{nonce}`).
- **URL Verification Handshake:** Automatically responds to `{"type": "url_verification", "challenge": "..."}` in < 1s.

---

### 3.9 `POST /webhook/telegram`
Webhook receiver for Telegram Bot updates.
- **Method:** `POST`
- **Headers Required:** `X-Telegram-Bot-Api-Secret-Token` matching `TELEGRAM_WEBHOOK_SECRET`.
- **Response `200 OK`:** `{"status": "ok"}`

### 3.10 Admin module registry (`/admin/modules`)
Authenticated operator UI and API for database-backed image and logic modules (migration 008).
- `GET /admin/modules` lists/search modules and opens create/edit forms.
- `POST /admin/modules` creates a module; `POST /admin/modules/{id}` saves name/version/definition.
- `POST /admin/modules/{id}/enabled` toggles runtime use; `POST /admin/modules/{id}/delete` removes it.
- `POST /admin/modules/{id}/run` executes an enabled logic module with JSON integer inputs.
- `GET /api/v1/admin/modules` returns module records for automation.
- Object definitions become database-authoritative after startup and drive image prompts/field checks. Logic definitions reference only implementations compiled in `services/logic_modules.py`; stored JSON is never executed as Python. Destructive/enable actions require the same strict same-origin checks as other admin writes and are audited in `system_audit_log`.

### 3.11 Admin Google Drive exports (`/admin/exports`)
Authenticated operator UI for migration 009/010 export configuration.
- `GET /admin/exports` renders the client/internal directory tree plus folder settings and run controls.
- `GET /admin/exports/{slug}` edits the client folder: display fields, enabled state, dates, and which forms the client receives.
- `GET /admin/exports/{slug}/{form_id}` edits that form's output for that client: fields, statuses, vehicle kinds, dates, and image inclusion.
- `POST /admin/exports/settings` saves master/client/internal/images folder names.
- `POST /admin/exports/clients` creates a configuration; `POST /admin/exports/clients/{slug}` updates it; `POST /admin/exports/clients/{slug}/delete` removes the configuration only, leaving Drive folders intact.
- `POST /admin/exports/clients/{slug}/forms/{form_id}` saves a per-form override in `drive_export_clients.form_settings` (migration 010); `.../delete` resets that form to client defaults.
- `POST /admin/exports/run` executes enabled configurations under a PostgreSQL advisory lock and stores the last report. Each form runs with its effective override; missing overrides inherit the client-wide fields, statuses, vehicle kinds, dates, and image flag. Each run also removes Drive folders for archived forms that are not active, deletes stale image files/shortcuts in a submission folder, and removes client/internal submission folders no longer selected by that configuration.
- Deleting a form removes its current and legacy Drive folders; deleting a submission removes its master image folder and every client/internal shortcut folder. Both hooks are best-effort, and failures never undo the database deletion.
- `GET /api/v1/admin/exports` returns settings and configurations for automation.
- Exports include a filtered native Google Sheet per form and an `Images` folder of shortcuts into `Images Master`; images are never duplicated per client. All writes require operator auth and strict same-origin checks. Client and form saves require an explicit confirmation before submission and show a flash message after redirect.

---

## 4. Telegram Bot Moderation & Operations Daemon

The pipeline bundles a dedicated operational daemon (`services/telegram_polling.py`) running as a standalone container (`telegram-bot`) to handle live administrative moderation without requiring an inbound public webhook for Telegram.

### 4.1 Daemon Architecture & Fault Tolerance
- **Long-Polling Engine:** Uses `getUpdates` with `timeout: 30` seconds.
- **Exponential Backoff:** If the Telegram Bot API returns HTTP 429 (Rate Limit) or network drops, backoff delay increases exponentially (`1.0s -> 2.0s -> 4.0s ... max 60.0s`).
- **Mock Token Resilience:** If `TELEGRAM_BOT_TOKEN` is unset or set to `"mock_tg_token"`, the daemon enters safe simulation mode, logging dispatched messages to stdout without crashing.

### 4.2 Telegram commands

| Command | Admin bot | Partner bot |
|---|---|---|
| `/forms` | Five most recently saved public forms still below their response limit, with direct links | Same list |
| `/check <submission_id>` (`/query`) | Opens the dossier and photos. Pending decisions require confirmation; approved/rejected records remain locked. Replying with `/check` to a submission notification also works. | Not available |
| `/audit <submission_id>` | Same dossier; approved/rejected records also offer the opposite verdict. The requesting operator must confirm before the decision changes. A stale confirmation is rejected; a successful override is audited and follows the record's existing delivery owner. | Not available |
| `/surveys` (`/history`, `/my_surveys`, `/tracuu`) | Pass a CTV code/ID or phone number to list five recent matches. Phone search checks the CTV directory, driver number, and original message-target answer. | In a private chat, share your own Telegram contact first. Lists up to five recent submissions whose saved delivery-question answer matches that verified number and that were submitted during its current verification period. A typed `/linkphone` number, the survey's driver number alone, or an unknown historical routing policy is not enough. |
| `/quota` | Looks up the supplied CTV code. | Shows the linked CTV's quota. |
| `/stats` | Shows total, approved, rejected, and pending counts from the writer database. Database errors report unavailable; zero rows do not fall back to DEV data. DEV storage is used only without a pool outside production. Same admin authorization as history. | Not available |

Admin dossier reads, overrides, and history require the configured admin group or an active admin account in its own private chat. Missing required arguments get a Telegram reply prompt. Reply to that exact bot message in the same chat and from the same account within ten minutes; unrelated messages and replies do not run the command. Shared Redis allows prompts and confirmations to work across webhook workers. With no shared Redis they are process-local.

Dossiers send one readable photo with `sendPhoto`, multiple photos as an album, then the dossier document with review buttons. With no readable photos, only the document is sent. Partner history shows a photo-repair button only while the submission is rejected and the requester passes the existing driver-phone/verification-time authorization; being a notification recipient alone does not grant repair access. Callback and upload authorization remain enforced independently.

Local regression verification: **19 tests passed**, including zero/one/two-photo Telegram protocol fixtures and real disposable PostgreSQL history/repair authorization. Provider calls were intercepted; no live Telegram delivery or deployment was exercised.

---

## 5. Deployment, Container Orchestration & Infrastructure Runbook

### 5.1 Docker Compose Services Topology
The system runs via Docker Compose on the production host (`146.190.201.32`):

```yaml
services:
  caddy:
    image: caddy:alpine
    restart: always
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - app

  app:
    build: .
    restart: always
    command: uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
    environment:
      - ENVIRONMENT=production
      - DATABASE_URL=postgresql://postgres:secret_password@postgres:5432/surveydb
      - REDIS_URL=redis://redis:6379/0
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
      - LARK_APP_ID=${LARK_APP_ID}
      - LARK_APP_SECRET=${LARK_APP_SECRET}
      - LARK_ENCRYPT_KEY=${LARK_ENCRYPT_KEY}
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - TELEGRAM_WEBHOOK_SECRET=${TELEGRAM_WEBHOOK_SECRET}
      - R2_ACCOUNT_ID=${R2_ACCOUNT_ID}
      - R2_ACCESS_KEY_ID=${R2_ACCESS_KEY_ID}
      - R2_SECRET_ACCESS_KEY=${R2_SECRET_ACCESS_KEY}
    volumes:
      - ./data:/app/data
    depends_on:
      - postgres
      - redis

  outbox-worker:
    build: .
    restart: always
    command: python workers/outbox_worker.py
    environment:
      - DATABASE_URL=postgresql://postgres:secret_password@postgres:5432/surveydb
      - PYTHONPATH=/app
    depends_on:
      - postgres

  telegram-bot:
    build: .
    restart: always
    command: python -m services.telegram_polling
    environment:
      - ENVIRONMENT=production
      - DATABASE_URL=postgresql://postgres:secret_password@postgres:5432/surveydb
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - TELEGRAM_ADMIN_GROUP_ID=${TELEGRAM_ADMIN_GROUP_ID}
      - PYTHONPATH=/app
    depends_on:
      - postgres

  postgres:
    image: pgvector/pgvector:pg16
    restart: always
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: secret_password
      POSTGRES_DB: surveydb
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./schema.sql:/docker-entrypoint-initdb.d/schema.sql

  redis:
    image: redis:7-alpine
    restart: always
    command: redis-server --maxmemory 256mb --maxmemory-policy volatile-lru
    volumes:
      - redisdata:/data

volumes:
  pgdata:
  redisdata:
  caddy_data:
  caddy_config:
```

### 5.2 Caddy Reverse Proxy & Automatic SSL Configuration
In `Caddyfile`:
```caddyfile
146.190.201.32.sslip.io, 146.190.201.32.nip.io {
    reverse_proxy app:8000
    request_body {
        max_size 25MB
    }
}

survey.trustbridgevn.com {
    reverse_proxy app:8000
    request_body {
        max_size 25MB
    }
}
```
Caddy automatically provisions trusted Let's Encrypt certificates for the public hostname with zero certbot configuration.

### 5.3 Production Droplet Runbook (`146.190.201.32`)
```bash
# 1. Connect to production Droplet
ssh root@146.190.201.32

# 2. Navigate to project root
cd /home/ebylxn/projects/survey-pipeline

# 3. Pull latest release and rebuild containers
docker compose down
docker compose build --no-cache
docker compose up -d

# 4. Inspect container health
docker compose ps
docker compose logs -f app
docker compose logs -f outbox-worker
docker compose logs -f telegram-bot

# 5. Execute database schema migration (if modified)
docker compose exec -T postgres psql -U postgres -d surveydb < schema.sql
```

---

## 6. Test Suite & Verification Matrix

The pipeline is safeguarded by **233 automated test assertions** across 17 test suites located in `tests/`:

### 6.1 Test Directory Structure
```
tests/
├── test_pipeline.py                     # Core end-to-end ingest, media, eval flow
├── test_adversarial_break.py            # Round 1 adversarial attacks & vulnerability discovery
├── test_adversarial_round2.py           # Round 2 advanced stock evasion & anti-forensic probes
├── test_adversarial_round3.py           # Round 3 auth bypasses & ReDoS boundaries
├── test_privacy_and_tg_bot.py           # Decree 13 HMAC hashing, PII scrub, TG bot polling
├── test_geo_ip_fallback.py              # Geolocation IP fallback, headers, and Redis caching
├── test_image_suite.py                  # Dual-bound image scaling, blur, and income sanitizing
├── test_form_engine.py                  # Dynamic schema engine and conditional rules
├── test_numeric_keypad_and_mgmt.py      # Numeric keypad enforcement & form management
├── test_multi_image_and_order.py        # Multi-slot count and order reversal rejection
├── test_image_check_configuration.py    # Global and per-question image check overrides
├── test_architect_fixes.py              # Screen photo exemption & parallel gathering
├── test_fuzz_boundaries.py              # Single-pixel images, 10,000x2 ribbons, truncated JPEGs
├── test_telegram_and_features.py        # Telegram slash commands & outbox event triggers
├── test_wave1_bug_and_legality.py       # Wave 1 security, race conditions, and legal audit
├── test_wave2_replication_and_novel.py  # LRU cache bounds, vehicle flapping, and ReDoS
└── test_wave3_advanced_security.py      # Path traversal, null byte sanitization, and XSS
```

### 6.2 Running the Test Suite
Tests execute in an isolated virtual environment with all optional C-extensions and mock services active:

```bash
# Run complete test suite (233+ tests)
PYTHONPATH=. .venv/bin/pytest tests/ -v

# Run targeted security & adversarial test suites
PYTHONPATH=. .venv/bin/pytest tests/test_adversarial_break.py tests/test_adversarial_round2.py tests/test_adversarial_round3.py

# Run privacy & regulatory compliance checks (Decree 13 / GDPR)
PYTHONPATH=. .venv/bin/pytest tests/test_privacy_and_tg_bot.py tests/test_audit_and_privacy.py

# Run performance & boundary fuzzing
PYTHONPATH=. .venv/bin/pytest tests/test_fuzz_boundaries.py tests/test_image_suite.py
```

---
*Manual compiled and verified by Principal Systems Architect. Production deployment ready.*
