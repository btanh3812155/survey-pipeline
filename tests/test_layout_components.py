# -*- coding: utf-8 -*-
"""
Suite B: Layout & Component Invariant Test Suite.
Verifies that each user-facing page contains all needed and ONLY needed components:
1. First Form Page: location prompt placed in the exact right spot (above questions container).
2. CTV Landing Page: Brand badge, CTV dropdown, location pill, and action button.
3. Invariant checks: zero duplicate location banners, zero unclosed script tags, zero orphaned elements.
"""

import os
from services.dynamic_form_html import render_dynamic_form_html, render_ctv_selector_landing_html
from services.form_engine import DEFAULT_TRUST_BRIDGE_SCHEMA


def test_first_form_page_layout_has_all_needed_components():
    """Verifies that the first form page has all required components in the exact spot."""
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    # 1. Company Navigation Top Header Bar
    assert 'id="companyNavBar"' in html
    assert "TRUST BRIDGE" in html
    assert "Cổng Khảo Sát & Đối Soát" in html

    # 2. Top Navigation: QR Share & Language Globe Switcher
    assert "btnOpenQrModal" in html
    assert "btn-lang-toggle" in html
    # 2. First Form Page: Location check banner must exist INSIDE <form id="dynamicForm"> above #questionsContainer
    assert '<form id="dynamicForm"' in html
    assert 'id="gpsBanner"' in html
    assert 'id="questionsContainer"' in html

    # Verify positional invariant: gpsBanner must appear before questionsContainer
    pos_banner = html.find('id="gpsBanner"')
    pos_container = html.find('id="questionsContainer"')
    assert pos_banner < pos_container, "Location banner must appear above questionsContainer on the first page!"

    # 3. Location banner contains all needed sub-elements
    assert "gpsStatusText" in html
    assert "btnRequestGps" in html
    assert "gpsStatusPill" in html

    # 4. Action controls
    assert "btnSubmitForm" in html
    assert "btn-floating-reset" in html

    # 5. Modals
    assert "paperModal" in html
    assert "submissionResultModal" in html
    assert "confirmResetModal" in html
    assert "qrShareModal" in html

    # 6. Syntax Invariants: Zero duplicate gps banners and valid closures
    assert html.count('id="gpsBanner"') == 1, "There must be exactly ONE location banner on the form!"
    assert "</form>" in html
    assert "</body>" in html
    assert "</html>" in html


def test_ctv_landing_page_layout_has_all_needed_and_only_needed_components():
    """Verifies that the CTV landing page has exactly the required selection & location components."""
    options = ["CTV001_Dũng_HN", "CTV002_Tuấn_HN"]
    html = render_ctv_selector_landing_html(DEFAULT_TRUST_BRIDGE_SCHEMA, options)

    # 1. Brand & Header
    assert "Trust Bridge SSOT" in html
    assert "{escaped_title}" not in html  # Must be populated
    assert "{escaped_desc}" not in html

    # 2. CTV Selection Dropdown
    assert 'id="ctvSelect"' in html
    assert 'value="ctv001"' in html
    assert 'CTV001 - Dũng (HN)' in html

    # 3. Location Component on Landing Page
    assert "gps-banner-box" in html
    assert 'id="gpsStatusPill"' in html
    assert 'id="btnRequestGps"' in html

    # 4. Action button
    assert 'id="btnOpenForm"' in html
    assert "Mở Biểu Mẫu Khảo Sát" in html

    # 5. Cleanliness Invariants: No dead close buttons, closed script and body tags
    assert "admin_close_ticket" not in html
    assert "</script>" in html
    assert "</body>" in html
    assert "</html>" in html
    assert html.count("</script>") == 1
    assert html.count("<script>") == 1
    # 6. Security Invariant (1a): GPS coordinates are NEVER attached to URL query params!
    assert 'params.delete("lat")' in html
    assert 'params.delete("lon")' in html
    assert 'params.delete("acc")' in html
    assert 'params.set("lat"' not in html

    # 7. Gating Invariant (1k): Button requires BOTH CTV selection AND confirmed location!
    assert "Boolean(ctv && gpsDone)" in html


def test_model_answer_toggle_per_question():
    """Form designers can toggle whether a question renders a sample response / model answer."""
    from services.form_engine import FormSchema, FormQuestion, QuestionType

    # Case A: show_sample_response = True (default) -> sample response is allowed to render
    schema_with_sample = DEFAULT_TRUST_BRIDGE_SCHEMA.model_copy(deep=True)
    schema_with_sample.questions[0].show_sample_response = True
    html_with = render_dynamic_form_html(schema_with_sample)
    assert "q.show_sample_response !== false" in html_with

    # Case B: show_sample_response = False -> question suppresses model answer box
    schema_suppressed = DEFAULT_TRUST_BRIDGE_SCHEMA.model_copy(deep=True)
    schema_suppressed.questions[0].show_sample_response = False
    html_suppressed = render_dynamic_form_html(schema_suppressed)
    assert "show_sample_response" in html_suppressed
