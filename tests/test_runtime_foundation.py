from fastapi.testclient import TestClient

from townlight_parks import __version__
from townlight_parks.main import app

client = TestClient(app)


def test_root_reports_honest_current_state():
    payload = client.get("/").json()
    assert payload["name"] == "Townlight Parks"
    assert payload["version"] == __version__
    assert "database-backed registration/maintenance workpapers" in payload["message"]
    assert "payments" in payload["message"]
    assert "not implemented yet" in payload["message"]


def test_health_reports_townlight_core_pin():
    assert client.get("/health").json() == {
        "status": "ok",
        "service": "townlight_parks",
        "version": "0.2.0",
        "townlight_core_version": "1.3.0.dev2",
    }


def test_public_ui_contains_version_boundaries_and_dependency():
    text = client.get("/townlight_parks").text
    assert "Townlight Parks v0.2.0" in text
    assert "No payments" in text
    assert "townlight_core==1.3.0.dev2" in text


def test_api_endpoints_return_deterministic_payloads():
    policy = client.post(
        "/api/v1/townlight_parks/policy-answer",
        json={"question": "facility rental"},
    ).json()
    assert policy["staff_review_required"] is True

    program = client.post(
        "/api/v1/townlight_parks/program-answer",
        json={
            "question": "teen events",
            "programs": [
                {
                    "title": "Teen tennis",
                    "audience": "teens",
                    "date": "2026-05-10",
                    "accessibility_note": "Accessible courts",
                }
            ],
        },
    ).json()
    assert program["accessibility_review_required"] is True

    registration = client.post(
        "/api/v1/townlight_parks/registration-assistance",
        json={
            "program": {
                "title": "Swim lessons",
                "audience": "youth",
                "registration_url": "https://parks.example/register",
                "accessibility_note": "Ramp access",
            }
        },
    ).json()
    assert registration["assistance_id"] is None

    triage = client.post(
        "/api/v1/townlight_parks/maintenance-triage",
        json={"issue": "trash bin full", "location": "Oak Park"},
    ).json()
    assert triage["triage_id"] is None
