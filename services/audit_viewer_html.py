# -*- coding: utf-8 -*-
"""
Static template loader for System-Wide Immutable Audit Log Viewer.
Decoupled into static/ui/audit_viewer.html for pure static client rendering.
"""

import os
from typing import List, Dict, Any, Optional
from core.state import STATIC_DIR


def render_audit_viewer_html(
    audit_logs: Optional[List[Dict[str, Any]]] = None,
    stats: Optional[Dict[str, int]] = None,
    environment: str = "production",
) -> str:
    """Loads and returns pure static audit viewer client."""
    html_file = os.path.join(STATIC_DIR, "ui", "audit_viewer.html")
    if os.path.exists(html_file):
        with open(html_file, "r", encoding="utf-8") as f:
            return f.read()
    return "<html><body>Audit Viewer</body></html>"
