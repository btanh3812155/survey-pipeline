"""Authenticated submission action inspection and operator commands."""

from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from psycopg.errors import LockNotAvailable
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

from core.state import get_db_pool, verify_admin_auth
from services.action_checklist import (
    ActionConflict, ActionNotFound, fetch_action_status, inspect_action, read_dev_submission,
    reconcile_legacy, resolve_action, retry_actions,
)

router = APIRouter(tags=["Action Checklist"])
HASH = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
LegacyEffect = Literal["R2_UPLOAD", "ADMIN_REVIEW", "PARTY_VERDICT", "LARK_SYNC", "GDRIVE_EXPORT", "BILLING"]


class ActionResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Literal["confirm_delivered", "confirm_not_delivered", "resend"]
    version: HASH
    evidence_sha256: HASH = Field(description="SHA-256 of the retained external evidence or operator decision record.")
    accept_duplicate_risk: StrictBool = False


class LegacyEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding: Literal["missing", "delivered", "unknown"]
    evidence_sha256: HASH = Field(description="SHA-256 of retained evidence for this exact effect and destination.")


class LegacyResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: HASH
    legacy_drained: StrictBool = Field(
        default=False, description="Attest the old worker, checklist, admin review and party notification paths are drained.",
    )
    evidence: dict[LegacyEffect, LegacyEvidence] = Field(default_factory=dict)


@asynccontextmanager
async def _operator_connection(submission_id):
    pool = get_db_pool()
    if pool is None:
        if read_dev_submission(submission_id):
            raise HTTPException(status_code=409, detail="READ_ONLY_LEGACY_JSON")
        raise HTTPException(status_code=503, detail="ACTION_DATABASE_UNAVAILABLE")
    try:
        async with pool.connection() as conn:
            yield conn
    except ActionNotFound as exc:
        if read_dev_submission(submission_id):
            raise HTTPException(status_code=409, detail="READ_ONLY_LEGACY_JSON") from exc
        raise HTTPException(status_code=404, detail="Không tìm thấy hồ sơ khảo sát hoặc tác vụ") from exc
    except ActionConflict as exc:
        detail = str(exc)
        raise HTTPException(status_code=503 if detail == "SCHEMA_002_REQUIRED" else 409, detail=detail) from exc
    except LockNotAvailable as exc:
        raise HTTPException(status_code=409, detail="ACTION_BUSY") from exc


@router.get("/api/v1/submissions/{submission_id}/actions")
async def get_submission_actions_endpoint(
    submission_id: str,
    admin_user: str = Depends(verify_admin_auth),
):
    """Retrieves current action checklist status for a submission."""
    db_pool = get_db_pool()
    status = await fetch_action_status(db_pool, submission_id)
    return {"submission_id": submission_id, "actions": status}


@router.post("/api/v1/submissions/{submission_id}/retry-actions")
async def retry_submission_actions_endpoint(
    submission_id: str,
    admin_user: str = Depends(verify_admin_auth),
):
    """Commit eligible definitive failures before acknowledging the existing retry command."""
    async with _operator_connection(submission_id) as conn:
        await retry_actions(conn, submission_id, admin_user)
        await conn.commit()
    return {"status": "retry_dispatched", "submission_id": submission_id}


@router.get("/api/v1/submissions/{submission_id}/actions/{event_id}/evidence")
async def get_action_evidence_endpoint(
    submission_id: str,
    event_id: UUID,
    admin_user: str = Depends(verify_admin_auth),
):
    async with _operator_connection(submission_id) as conn:
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return {"submission_id": submission_id, **await inspect_action(conn, submission_id, event_id)}


@router.post("/api/v1/submissions/{submission_id}/actions/{event_id}/reconcile")
async def reconcile_action_endpoint(
    submission_id: str,
    event_id: UUID,
    resolution: ActionResolution,
    admin_user: str = Depends(verify_admin_auth),
):
    async with _operator_connection(submission_id) as conn:
        resend_event_id = await resolve_action(conn, submission_id, event_id, admin_user, **resolution.model_dump())
        await conn.commit()
    result = {"status": "resend_queued" if resend_event_id else "reconciled",
              "submission_id": submission_id, "event_id": str(event_id)}
    if resend_event_id:
        result["resend_event_id"] = resend_event_id
    return result


@router.post("/api/v1/submissions/{submission_id}/actions/reconcile-legacy")
async def reconcile_legacy_actions_endpoint(
    submission_id: str,
    resolution: LegacyResolution,
    admin_user: str = Depends(verify_admin_auth),
):
    """Classify legacy parents, or transfer only externally verified missing effects."""
    async with _operator_connection(submission_id) as conn:
        child_ids = await reconcile_legacy(conn, submission_id, admin_user, **resolution.model_dump())
        await conn.commit()
    return {"status": "reconciled" if child_ids else "quarantined", "submission_id": submission_id,
            "child_ids": child_ids}
