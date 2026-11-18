# -*- coding: utf-8 -*-
"""Canonical submission-id handling for commands, callbacks, and deep links."""

import re


def normalize_submission_id(value) -> str:
    """Return the canonical uppercase id used in storage and displays.

    Accepts ``vn-fd45...``, ``#vn-fd45...`` and whitespace-padded variants.
    """
    return re.sub(r"\s+", "", str(value or "")).lstrip("#").upper()
