# -*- coding: utf-8 -*-
"""
Decoupled Domain Routers for Survey Pipeline.
"""

from .system import router as system_router
from .admin_forms import router as admin_forms_router
from .admin_data import router as admin_data_router
from .admin_audit import router as admin_audit_router
from .admin_media import router as admin_media_router
from .admin_ctvs import router as admin_ctvs_router
from .admin_image_objects import router as admin_image_objects_router
from .actions import router as actions_router
from .webhooks import router as webhooks_router
from .survey import router as survey_router

__all__ = [
    "system_router",
    "admin_forms_router",
    "admin_data_router",
    "admin_audit_router",
    "admin_media_router",
    "admin_ctvs_router",
    "admin_image_objects_router",
    "actions_router",
    "webhooks_router",
    "survey_router",
]
