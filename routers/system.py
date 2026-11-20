# -*- coding: utf-8 -*-
"""
System & Infrastructure Utilities Router.
Handles Caddy on-demand TLS qualification, health checks,
geolocation IP fallback, and root subdomain redirects.
"""

import os
import json
import logging
import ipaddress
from typing import Optional
import httpx
from fastapi import APIRouter, Request, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from config import settings
from core.state import FORM_REGISTRY, get_db_pool, get_redis_client, get_http_client, refresh_form_registry_from_disk
from core.schema_gate import MIGRATION_001_CHECKSUM, REQUIRED_WRITER_TABLES, writer_schema_state
from services.dynamic_form_html import render_dynamic_form_html

logger = logging.getLogger("routers.system")
router = APIRouter(tags=["System"])

DEFAULT_GEO_FALLBACK = {
    "status": "success",
    "latitude": 21.0285,
    "longitude": 105.8542,
    "accuracy": 5000.0,
    "city": "Hanoi",
    "source": "ip_fallback",
}


@router.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    """Liveness only: do not use this endpoint to gate traffic or writes."""
    return {"status": "healthy", "environment": settings.ENVIRONMENT}




@router.get("/ready")
async def readiness_check():
    """Readiness requires the enabled PostgreSQL writer and its current schema."""
    db_pool = get_db_pool()
    if db_pool is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "reason": "postgresql_unavailable"},
        )
    try:
        async with db_pool.connection() as conn:
            state = await writer_schema_state(conn)
        missing = state["missing"]
        missing_columns = state["missing_columns"]
        migration_checksum = state["migration_checksum"]
        if missing:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "not_ready",
                    "reason": "schema_incomplete",
                    "missing": missing,
                },
            )
        if (
            missing_columns
            or not migration_checksum
            or not MIGRATION_001_CHECKSUM
            or migration_checksum != MIGRATION_001_CHECKSUM
        ):
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "not_ready",
                    "reason": "schema_incomplete",
                    "missing_columns": sorted(missing_columns),
                    "missing_migration": "001" if not migration_checksum else None,
                },
            )
    except Exception as exc:
        logger.warning("[READY] PostgreSQL readiness check failed: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "reason": "postgresql_unavailable"},
        )
    return {"status": "ready", "environment": settings.ENVIRONMENT}


@router.get("/api/v1/caddy-check", include_in_schema=False)
async def caddy_on_demand_tls_check(domain: str):
    """Verifies domain eligibility for Caddy on-demand automatic TLS certificates."""
    d = domain.lower().strip()
    if d.endswith(".sslip.io") or d.endswith(".nip.io") or d.endswith(".trustbridgevn.com") or d == "trustbridgevn.com":
        return HTMLResponse(content="OK", status_code=200)
    return HTMLResponse(content="Forbidden", status_code=403)


@router.get("/api/v1/geo/ip-fallback")
async def geo_ip_fallback(request: Request):
    """Provides IP-based geolocation fallback when browser navigator.geolocation fails."""
    try:
        cf_lat = request.headers.get("cf-iplatitude")
        cf_lon = request.headers.get("cf-iplongitude")
        if cf_lat and cf_lon:
            try:
                return {
                    "status": "success",
                    "latitude": float(cf_lat),
                    "longitude": float(cf_lon),
                    "accuracy": 5000.0,
                    "city": str(request.headers.get("cf-ipcity") or "Vietnam"),
                    "source": "cloudflare_edge",
                }
            except (ValueError, TypeError):
                pass

        client_ip = request.headers.get("cf-connecting-ip")
        if not client_ip:
            x_forwarded = request.headers.get("x-forwarded-for")
            if x_forwarded:
                client_ip = x_forwarded.split(",")[0].strip()
        if not client_ip and request.client:
            client_ip = request.client.host

        if not client_ip or client_ip in ("127.0.0.1", "localhost", "::1", "testclient"):
            return DEFAULT_GEO_FALLBACK

        try:
            ip_obj = ipaddress.ip_address(client_ip)
            if ip_obj.is_private or ip_obj.is_loopback:
                return DEFAULT_GEO_FALLBACK
        except ValueError:
            return DEFAULT_GEO_FALLBACK

        redis_client = get_redis_client()
        redis_key = f"geo_cache:{client_ip}"
        if redis_client:
            try:
                cached = await redis_client.get(redis_key)
                if cached:
                    cached_data = json.loads(cached)
                    return {
                        "status": "success",
                        "latitude": float(cached_data.get("latitude", DEFAULT_GEO_FALLBACK["latitude"])),
                        "longitude": float(cached_data.get("longitude", DEFAULT_GEO_FALLBACK["longitude"])),
                        "accuracy": float(cached_data.get("accuracy", DEFAULT_GEO_FALLBACK["accuracy"])),
                        "city": str(cached_data.get("city", DEFAULT_GEO_FALLBACK["city"])),
                        "source": "ip_fallback",
                    }
            except Exception as e:
                logger.warning(f"Redis geo cache lookup failed: {e}")

        url = f"http://ip-api.com/json/{client_ip}?fields=status,country,city,lat,lon"
        res_data = None
        http_client = get_http_client()
        try:
            if http_client is not None and getattr(http_client, "is_closed", None) is not True:
                resp = await http_client.get(url, timeout=2.5)
            else:
                async with httpx.AsyncClient(timeout=2.5) as client:
                    resp = await client.get(url, timeout=2.5)
            if resp.status_code == 200:
                res_data = resp.json()
        except Exception as e:
            logger.warning(f"ip-api lookup failed for {client_ip}: {e}")

        if res_data and res_data.get("status") == "success":
            try:
                lat = float(res_data["lat"])
                lon = float(res_data["lon"])
                city = str(res_data.get("city") or DEFAULT_GEO_FALLBACK["city"])
                result = {
                    "status": "success",
                    "latitude": lat,
                    "longitude": lon,
                    "accuracy": 5000.0,
                    "city": city,
                    "source": "ip_fallback",
                }
                if redis_client:
                    try:
                        await redis_client.set(redis_key, json.dumps(result), ex=86400)
                    except Exception as e:
                        logger.warning(f"Redis geo cache set failed: {e}")
                return result
            except Exception as e:
                logger.warning(f"Failed to parse ip-api response: {e}")
        return DEFAULT_GEO_FALLBACK
    except Exception as e:
        logger.warning(f"Unhandled exception in geo_ip_fallback: {e}")
        return DEFAULT_GEO_FALLBACK


@router.get("/samples/{filename}")
async def get_sample_image(filename: str):
    """Serves public sample testing images with path traversal protection."""
    if not filename or "\x00" in filename or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename format")
    samples_dir = os.path.join(os.getcwd(), "data", "test_samples")
    safe_path = os.path.realpath(os.path.join(samples_dir, filename))
    base_dir = os.path.realpath(samples_dir)
    try:
        common = os.path.commonpath([safe_path, base_dir])
        if common != base_dir or not os.path.isfile(safe_path):
            raise HTTPException(status_code=404, detail="Sample image not found")
    except (ValueError, TypeError, OSError):
        raise HTTPException(status_code=404, detail="Sample image not found")
    ext = os.path.splitext(filename)[1].lower()
    media_type = "image/jpeg" if ext in (".jpg", ".jpeg") else ("image/png" if ext == ".png" else "image/webp")
    return FileResponse(safe_path, media_type=media_type)


@router.get("/", include_in_schema=False)
async def root_redirect(request: Request):
    """Auto-redirects root URL to /forms/income_survey or matches form from subdomain."""
    refresh_form_registry_from_disk()
    host = (request.headers.get("host") or "").lower()
    if any(host.startswith(p) for p in ("audit.", "logs.", "dev-audit.", "audit-dev.", "audit.dev.")):
        return RedirectResponse(url="/admin/audit", status_code=status.HTTP_302_FOUND)
    if any(host.startswith(p) for p in ("admin.", "dev-admin.", "admin-dev.", "admin.dev.")):
        return RedirectResponse(url="/admin/forms", status_code=status.HTTP_302_FOUND)
    if any(host.startswith(p) for p in ("view.", "data.", "dev-data.", "data-dev.", "data.dev.")):
        return RedirectResponse(url="/admin/submissions", status_code=status.HTTP_302_FOUND)

    sub_label = host.split(":")[0].split(".")[0]
    parts = sub_label.split("-")
    if len(parts) >= 2:
        candidate_slug = "_".join(parts[:-1])
        if candidate_slug in FORM_REGISTRY:
            schema = FORM_REGISTRY[candidate_slug]
            return HTMLResponse(content=render_dynamic_form_html(schema), status_code=200)
        if parts[0] in FORM_REGISTRY:
            schema = FORM_REGISTRY[parts[0]]
            return HTMLResponse(content=render_dynamic_form_html(schema), status_code=200)

    preferred_form_id = (
        "TB_GSM_2026_Nov_Test"
        if "TB_GSM_2026_Nov_Test" in FORM_REGISTRY
        else next(iter(FORM_REGISTRY), "income_survey")
    )
    query_str = str(request.url.query)
    target = f"/forms/{preferred_form_id}" + (f"?{query_str}" if query_str else "")
    return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
