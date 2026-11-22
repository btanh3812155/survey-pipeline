"""Directory-tree export controls and explicit save confirmations."""
from routers.admin_exports import _render_client, _render_form, _render_root

CLIENT = {
    "slug": "acme",
    "display_name": "ACME",
    "kind": "client",
    "folder_name": "ACME",
    "enabled": True,
    "date_from": None,
    "date_to": None,
    "form_ids": ["TB_GSM_2026_Nov_Test"],
    "fields": ["submission_id", "status"],
    "statuses": ["APPROVED"],
    "vehicle_types": [],
    "include_images": True,
    "form_settings": {
        "TB_GSM_2026_Nov_Test": {
            "fields": ["submission_id"],
            "statuses": ["APPROVED"],
            "vehicle_types": ["CAR"],
            "include_images": False,
            "date_from": None,
            "date_to": None,
        }
    },
}
SETTINGS = {
    "master_folder_name": "Master Export",
    "client_folder_name": "Client Exports",
    "internal_folder_name": "Internal Exports",
    "images_folder_name": "Images Master",
    "last_report": None,
}


def test_root_renders_navigable_client_and_form_folders():
    html = _render_root(SETTINGS, [CLIENT])
    assert "Directory tree" in html
    assert 'href="/admin/exports/acme"' in html
    assert 'href="/admin/exports/acme/TB_GSM_2026_Nov_Test"' in html
    assert "custom output" in html


def test_client_page_edits_received_forms_and_confirms_save():
    html = _render_client(CLIENT)
    assert "Forms this folder receives" in html
    assert 'name="form_ids"' in html
    assert 'value="TB_GSM_2026_Nov_Test"' in html
    assert "return confirm('Save changes to acme?')" in html
    assert "Save client and form selection" in html
    assert 'href="/admin/exports/acme/TB_GSM_2026_Nov_Test"' in html


def test_form_page_edits_output_and_confirms_save_and_reset():
    html = _render_form(CLIENT, "TB_GSM_2026_Nov_Test")
    assert "Output settings" in html
    assert 'name="fields"' in html
    assert 'name="statuses"' in html
    assert 'name="vehicle_types"' in html
    assert 'name="include_images"' in html
    assert "return confirm('Save output settings for acme / TB_GSM_2026_Nov_Test?')" in html
    assert "Reset to client defaults" in html
    assert "Reset this form to client defaults?" in html
