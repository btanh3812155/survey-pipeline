"""Directory-tree export controls and explicit save confirmations."""
from routers.admin_exports import _render_client, _render_client_exports, _render_form, _render_root

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
    "client_folder_name": "clients",
    "internal_folder_name": "internal",
    "images_folder_name": "master images",
    "last_report": None,
}


def test_root_renders_drive_folder_browser():
    html = _render_root(SETTINGS, [CLIENT])
    assert 'class="drive-top"' in html
    assert "Drive của tôi" in html
    assert 'href="/admin/exports/client-exports"' in html
    assert 'href="/admin/exports/internal-exports"' in html
    assert "clients" in html
    assert "internal" in html


def test_client_exports_folder_has_edit_clients_section():
    html = _render_client_exports(SETTINGS, [CLIENT], "client")
    assert "Sửa khách hàng" in html
    assert 'id="newClientForm"' in html
    assert 'href="/admin/exports/acme"' in html
    assert "Khách hàng mới" in html
    assert 'class="details"' in html


def test_all_forms_client_still_shows_form_folders():
    client = dict(CLIENT, form_ids=[], form_settings={})
    html = _render_client(client)
    assert 'href="/admin/exports/acme/' in html
    assert "mặc định khách hàng" in html


def test_client_page_edits_received_forms_and_confirms_save():
    html = _render_client(CLIENT)
    assert "Quản lý form của khách hàng này" in html
    assert 'name="form_ids"' in html
    assert 'value="TB_GSM_2026_Nov_Test"' in html
    assert "return confirm('Lưu thay đổi cho acme?')" in html
    assert "Lưu khách hàng và form" in html
    assert 'href="/admin/exports/acme/TB_GSM_2026_Nov_Test"' in html


def test_form_page_edits_output_and_confirms_save_and_reset():
    html = _render_form(CLIENT, "TB_GSM_2026_Nov_Test")
    assert "Khách hàng này được truy cập gì" in html
    assert 'name="fields"' in html
    assert 'name="statuses"' in html
    assert 'name="vehicle_types"' in html
    assert 'name="include_images"' in html
    assert 'name="date_from_mode"' in html
    assert 'name="date_to_mode"' in html
    assert "Kế thừa khoảng của khách hàng" in html
    assert "Không giới hạn từ ngày" in html
    assert "return confirm('Lưu thiết lập đầu ra cho acme / TB_GSM_2026_Nov_Test?')" in html
    assert "Đặt lại mặc định khách hàng" in html
    assert "Đặt lại form này về mặc định khách hàng?" in html


def test_form_date_override_can_clear_a_client_range():
    client = dict(
        CLIENT,
        date_from="2026-01-01",
        date_to="2026-12-31",
        form_settings={
            "TB_GSM_2026_Nov_Test": {
                "fields": ["submission_id"],
                "statuses": ["APPROVED"],
                "vehicle_types": [],
                "include_images": False,
                "date_from": None,
                "date_to": None,
                "date_from_clear": True,
                "date_to_clear": False,
            }
        },
    )
    html = _render_form(client, "TB_GSM_2026_Nov_Test")
    assert 'value="clear" checked' in html
    assert "Kế thừa khoảng của khách hàng (2026-01-01)" in html
    assert "Không giới hạn từ ngày" in html
