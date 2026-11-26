# Trust Bridge Survey Pipeline - Documentation Hub

> **For clients and LLM assistants:** This directory contains the authoritative **user manuals**, **campaign templates**, and **current technical architecture** for Trust Bridge Survey Verification Pipeline. Use the routing table below to load the exact document needed to answer a question accurately without guessing.

## Quick Routing Guide (Human & LLM Index)

| If you are asking about... | Primary Document | Audience |
|---|---|---|
| CTV onboarding, Telegram verification, changing Telegram accounts, `/ctv` OTP login, guiding drivers, GPS/camera capture, handling rejections, web/Telegram appeals, retry links, support chats | [HUONG_DAN_CTV.md](HUONG_DAN_CTV.md) | CTVs & LLM support assistants |
| Daily admin & superadmin operations: forms, modules, CTV directory & survey assignments, `/admin/appeals`, `/admin/payouts`, `/admin/exports` (Google Drive), `/admin/audit`, accounts, trash, Telegram moderation | [HUONG_DAN_QUAN_TRI.md](HUONG_DAN_QUAN_TRI.md) | Staff (`superadmin`, `manager`, `reviewer`, `viewer`) |
| Launching a new survey campaign (photo requirements, quotas, limits, assignments, escalation) | [MAU_HUONG_DAN_CHIEN_DICH.md](MAU_HUONG_DAN_CHIEN_DICH.md) | Campaign managers & CTV onboarding |
| System architecture, database schema & migrations (`001`-`017`), RBAC & multi-role rules, HTTP/API routes, Telegram commands, state machines, error-code troubleshooting, security boundaries | [TECHNICAL_ARCHITECTURE.md](TECHNICAL_ARCHITECTURE.md) | Engineers, technical reviewers & LLM coding/support assistants |
| Guarded deployment, migrations, rollback & smoke testing | [DEPLOY_TOOLING.md](DEPLOY_TOOLING.md) | Release operators (`superadmin` / DevOps) |

## Core Operational Principles (Summary for Fast Retrieval)

1. **Source of truth**: PostgreSQL is the authoritative source of truth for CTVs, assignments, submissions, appeals, audit logs, and billing ledgers. Google Sheets (`CTV_Info` and Drive exports) are **outputs**, not places to edit identity.
2. **Role boundaries**:
   - **Web staff roles (`admin_accounts`)**: `superadmin` (oversight, accounts, recovery, trash, payouts/revocations, audit + full ops), `manager` (daily ops: forms, CTVs, assignments, appeals, modules, exports, and audit), `reviewer` (read submissions + approve/reject web appeals), `viewer` (read-only).
   - **Telegram multi-role (`authorized_users.roles`)**: A single Telegram account may hold multiple roles (`ADMIN`, `COLLABORATOR`, `DRIVER`) simultaneously without losing admin privileges or CTV billing eligibility.
   - **Drivers** are outside actors guided by contracted **CTVs** (payees); no standalone driver manual is required.
3. **Survey access & limits**:
   - `access_control = "private"` blocks unauthenticated visitors (`403`) across form HTML, schema API, and submit API; authenticated staff can still preview/test.
   - CTV-tracked surveys enforce **assigned-only access** (`ctv_form_assignments`) across `/ctv`, direct/bookmarked links, selector dropdowns, Telegram `/forms`, and submission time. Active non-test CTVs are auto-assigned by default when new forms or non-test CTVs are created; revoking an assignment blocks submission immediately (`403`).
   - **CTV quotas** are tracking-only progress goals and count `APPROVED` submissions only.
   - **Campaign `response_limit`** counts `APPROVED` submissions only; when reached, the form page shows a limit-reached notice and submission returns `409`.
4. **Verification, GPS, Appeals & Payouts**:
   - Non-test submissions hard-require valid GPS coordinates (`400` if missing/invalid).
   - Manual review vs auto-approval is controlled per form via `settings.require_manual_review` (default `true`).
   - Web appeals (`/admin/appeals`) and Telegram appeals (`Phúc khảo #ID`) are interchangeable; regular manual-review approvals and CTV support chats are Telegram-only.
   - Payout settlement is currently offline; `superadmin` can update settlement status and revoke `APPROVED`/`PAID` rows on `/admin/payouts`, with every change recorded in `system_audit_log`.

## Historical & Archive References

Older audit reports, wave implementation plans, and baseline records remain in `docs/` for traceability (for example, `MODULE_IMPLEMENTATION_PLANS.md`, `WAVE2_H1_PHONE_BOUNDARY.md`, `WAVE3_IDEMPOTENCY_MEDIA_BOUNDARY.md`, `PIPELINE_OPERATIONS_AND_API_REFERENCE.md`, `BUGS_AND_SAFEGUARDS_ARCHIVE.md`). When an older document conflicts with [TECHNICAL_ARCHITECTURE.md](TECHNICAL_ARCHITECTURE.md) or [HUONG_DAN_QUAN_TRI.md](HUONG_DAN_QUAN_TRI.md), treat the current documents above as authoritative.
