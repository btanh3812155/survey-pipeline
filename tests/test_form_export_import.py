"""Form JSON export/import keeps scoped module configuration intact."""
from fastapi.testclient import TestClient

from core.state import verify_admin_auth
from main import app

ORIGIN = {"Origin": "http://testserver"}
FORM_ID = "EXPORT_IMPORT_TEST"


def _admin():
    app.dependency_overrides[verify_admin_auth] = lambda: "admin"


def _clear_admin():
    app.dependency_overrides.pop(verify_admin_auth, None)


def _schema():
    return {
        "form_id": FORM_ID,
        "title": "Export import test",
        "settings": {
            "modules": [
                {
                    "uid": "qcheck",
                    "name": "Name check",
                    "kind": "engine",
                    "module_id": "required_fields",
                    "scope": "question",
                    "question_id": "name",
                    "inputs": [
                        {"name": "fields", "source": "literal", "literal": ["name"]}
                    ],
                    "outputs": [{"key": "passed", "name": "name_ok"}],
                }
            ]
        },
        "questions": [
            {"id": "name", "title": "Name", "type": "text", "required": True}
        ],
    }


def test_export_import_round_trip_and_overwrite_guard():
    client = TestClient(app)
    _admin()
    try:
        created = client.post("/api/v1/forms/upload-schema", json=_schema(), headers=ORIGIN)
        assert created.status_code == 200, created.text

        exported = client.get(f"/api/v1/forms/{FORM_ID}/export")
        assert exported.status_code == 200
        bundle = exported.json()
        assert bundle["format"] == "survey-pipeline-forms"
        assert bundle["version"] == 1
        assert bundle["forms"][0]["settings"]["modules"][0]["scope"] == "question"
        assert bundle["forms"][0]["settings"]["modules"][0]["question_id"] == "name"

        assert client.delete(f"/api/v1/forms/{FORM_ID}", headers=ORIGIN).status_code == 200

        imported = client.post("/api/v1/forms/import", json=bundle, headers=ORIGIN)
        assert imported.status_code == 200, imported.text
        assert imported.json()["count"] == 1

        conflict = client.post("/api/v1/forms/import", json=bundle, headers=ORIGIN)
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["conflicts"] == [FORM_ID]

        overwritten = client.post(
            "/api/v1/forms/import?overwrite=true", json=bundle, headers=ORIGIN
        )
        assert overwritten.status_code == 200, overwritten.text

        all_forms = client.get("/api/v1/forms/export")
        assert all_forms.status_code == 200
        assert FORM_ID in {form["form_id"] for form in all_forms.json()["forms"]}
    finally:
        client.delete(f"/api/v1/forms/{FORM_ID}", headers=ORIGIN)
        _clear_admin()
