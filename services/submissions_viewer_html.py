# -*- coding: utf-8 -*-
"""
Static template loader for Submissions Data Hub Viewer.
Decoupled into static/ui/submissions_viewer.html for pure static client rendering.
"""

import os
from typing import List, Dict, Any, Optional
from core.state import STATIC_DIR


def render_submissions_viewer_html(
    submissions: Optional[List[Dict[str, Any]]] = None,
    forms: Optional[Dict[str, Any]] = None,
    selected_form_id: Optional[str] = None,
    environment: str = "production",
) -> str:
    """Loads and returns pure static submissions viewer client."""
    html_file = os.path.join(STATIC_DIR, "ui", "submissions_viewer.html")
    if os.path.exists(html_file):
        with open(html_file, "r", encoding="utf-8") as f:
            return f.read()
    return "<html><body>Submissions Viewer</body></html>"
