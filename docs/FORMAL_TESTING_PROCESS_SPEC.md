# Standardized Formal Testing Specification & Verification Protocol

**Authoritative Formal Testing Process, Threat Models, Gap Analysis, and Invariant Verification**  
*Documenting user manual verification workflows as standardized simulated automated tests.*

---

## 1. Phase 1: CTV Landing & Referral Ingress Protocol (Steps 1a – 1k)

### Technical Context & Invariant Definitions
The CTV selection page (`/forms/{form_id}` accessed without a referral parameter) serves as the primary gateway for field surveys. It ensures that every submission is legitimately associated with an authorized Collaborator (CTV) and tied to a verified geographic operating area.

| Step | Formal Test Identifier | Description & Invariant | Gap Analysis & Security Resolution |
|---|---|---|---|
| **1a** | `SEC-GPS-TAMPER-01` | **No Geolocation in URL Query Parameters**: Geolocation coordinates (`lat`, `lon`, `acc`) must **never** be exposed or passed via URL query strings. | **Gap Identified**: Coordinates in URL parameters can be manipulated by malicious actors to spoof locations. **Resolution**: Coordinates are stored in encrypted/isolated `sessionStorage` and retrieved directly on the form. URLs contain strictly referral identity (`?ref={ctv}`). |
| **1b** | `UX-GPS-PERSIST-01` | **Persistent Permission State**: Once granted, permission state is remembered across navigations in `localStorage('tb_gps_granted')`. | Re-navigating to the CTV page reuses verified permission without prompting again. |
| **1c** | `UI-CTV-CARD-01` | **Card Component Invariant**: Clean responsive container with company badge (`Trust Bridge SSOT`), title, description, and high-contrast typography. | Verified across 360px–1440px viewports with zero horizontal overflow. |
| **1d** | `UX-CTV-SELECT-01` | **Dropdown Enumeration**: Options populated strictly with registered active CTV short codes and locations (`CTV001 - Dũng (HN)`). | Default state must be empty (`value=""`). |
| **1e** | `NAV-CTV-REDIRECT-01` | **Handoff Navigation**: Clicking action button redirects to form URL with sanitized `ref={ctv_code}` and clean query parameters. | Guaranteed path preservation (`/forms/{form_id}?ref={ctv}`). |
| **Note**| `SPEC-ACC-EXPLANATION` | **Clarification of `acc` parameter**: `acc` represents the GPS Estimated Position Error (accuracy radius) in meters, conforming to W3C Geolocation API `Coordinates.accuracy`. | Value `15` denotes an accuracy radius of 15 meters (high-precision terrestrial GPS fix). Value `5000` indicates a coarse IP-based regional fallback fix. |
| **1f** | `GATE-BTN-DISABLED-01` | **Gated Action Button (CTV Missing)**: If no CTV is selected (`value == ""`), the action button is disabled (`disabled=true`, `opacity: 0.45`, `cursor: not-allowed`). | Form cannot be entered anonymously without CTV assignment. |
| **1g** | `UX-INCOGNITO-PROMPT` | **Clean-Session Geolocation Handshake**: In private/incognito mode with cleared storage, browser is prompted for location permission. | Native browser prompt must trigger upon user intent. |
| **1h** | `DATA-GPS-PRECISION-01`| **Hardware GPS Resolution**: When allowed, latitude and longitude are obtained from device GPS sensors. | Pill UI updates to green `✓ Đã xác thực GPS (lat, lon)`. |
| **1i** | `UX-GPS-DENIAL-01` | **Graceful Denial Handling**: Denying browser prompt dismisses native popup while leaving the webpage banner active for manual retry. | No hard page crash; retry button remains interactive. |
| **1j** | `SEC-FALLBACK-DISCIPLINE`| **User-Initiated Fallback Only**: Passive page loads must **never** auto-fallback to IP location. Fallback to IP is permitted **only** when the user explicitly clicks "Bật vị trí" and hardware GPS times out after 2.0s. | **Gap Identified**: Previous implementation fired IP fallback immediately on load. **Resolution**: Enforced strict user-action trigger (`requestGps()`) with a 2-second timeout before IP fallback engages. |
| **1k** | `GATE-DUAL-CONDITION-01`| **Dual-Gated Form Ingress**: The action button `[Mở Biểu Mẫu Khảo Sát]` remains disabled unless **BOTH** (1) CTV is selected **AND** (2) Location is confirmed (GPS or user-initiated fallback). | **Gap Identified**: Button previously enabled when only CTV was selected. **Resolution**: Gated on `ctv !== "" && gpsDone === true`. |

---

## 2. Phase 2: Form Intake, Verification & Interactive UX Protocol (Steps 2a – 2f)

| Step | Formal Test Identifier | Description & Invariant | Gap Analysis & Security Resolution |
|---|---|---|---|
| **2a** | `UI-FORM-LAYOUT-01` | **Form Layout & Component Invariant**: Clean top bar with company navigation branding, top-right QR share & language switcher (`VI`/`EN`), form title, location status toast, floating reset and Telegram help buttons. | **Gap Identified**: Form lacked a persistent company navigation bar. **Resolution**: Implemented persistent company branding header bar for future CTV multi-page routing. |
| **2b** | `VAL-SIGNATURE-MODAL-01`| **Paper Signature Modal Verification**: Submitting without signature is denied. Tapping signature opens paper canvas with Clear, Close, and Confirm buttons. Signing then closing cancels without saving. Clear button resets canvas. Confirming empty canvas is rejected. Re-opening canvas after confirming retains prior signature unless re-cleared and confirmed. | Prevents corrupted, blank, or accidentally discarded signatures. |
| **2c** | `VAL-CONSENT-MANDATORY` | **Mandatory Decree 13 Consent**: Submitting with signature but without answering Question 2 (Consent) is rejected. | Complies strictly with Decree 13 Article 11. |
| **2d** | `COND-EXPAND-QUESTIONS` | **Progressive Disclosure on Consent**: Selecting "Tôi đồng ý" removes the initial gate banner and reveals all remaining survey questions. | Enforces chronological completion. |
| **2e** | `COND-COLLAPSE-QUESTIONS`| **Instant Question Revocation on Dissent**: Changing answer to "Tôi không đồng ý" immediately hides all survey questions and restores the consent gate banner. | Minimizes personal data exposure. |
| **2f** | `COND-VEHICLE-SUBTYPE` | **Vehicle Dynamic Branching**: Selecting "Ô tô" reveals dependent car model question (`car_model`). Selecting "Xe máy" keeps it hidden. | Dynamic schema conditional branching. |
| **Note**| `SPEC-MODEL-ANSWER-TOGGLE`| **Per-Question Sample Response Toggle**: Ability for form designers to toggle whether a sample answer / model response is visible for each question. | **Resolution**: Added `show_sample_response: bool = True` to `FormQuestion` schema and frontend template. |

---

## 3. High-Priority Queued Test Development Program

The following test suites are prioritized for immediate development:
1. **`tests/test_image_checking.py`**:
   - Order-agnostic multi-image slot verification (Profile, Vehicle, 5 Weeks of Income).
   - Real-world GSM photo validation, DINOv2 vs distinct OCR metrics override.
   - Monthly summary upload rejection guard.
2. **`tests/test_layout_components.py`**:
   - Exact component verification across all pages (CTV landing, form intake, media manager, data hub, audit trail).
   - Zero extraneous, duplicate, or dead elements.
3. **`tests/test_schema_sync_responses.py`**:
   - Component reactivity to incoming schema modifications across all pipeline layers.
4. **`tests/test_user_facing_behavior.py`**:
   - Comprehensive multi-party user simulation: CTV selection, touch signature drawing, Telegram 2-way admin routing, 1-admin-1-convo cap, and two-step decision confirmation.
