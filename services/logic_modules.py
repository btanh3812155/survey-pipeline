"""Small, allowlisted logic-module implementations. Registry data never supplies Python code."""
from __future__ import annotations

from typing import Any


def get_percentage(inputs: dict[str, Any]) -> dict[str, float | int]:
    target = inputs["target"]
    total = inputs["total"]
    return {"percentage": (target * 100.0 / total) if total else 0.0}


IMPLEMENTATIONS = {"get_percentage": get_percentage}
SOURCES = {
    "get_percentage": "def get_percentage(inputs):\n    target = inputs['target']\n    total = inputs['total']\n    return {'percentage': target * 100.0 / total if total else 0.0}\n"
}


def default_get_percentage_definition() -> dict[str, Any]:
    return {
        "id": "get-percentage",
        "name": "get-percentage",
        "version": "1",
        "description": "Calculate progress as target / total * 100; returns 0 when total is zero.",
        "implementation": "get_percentage",
        "inputs": [
            {"name": "target", "type": "integer", "required": True},
            {"name": "total", "type": "integer", "required": True},
        ],
    }
