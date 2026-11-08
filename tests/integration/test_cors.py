"""The dashboard runs on another origin, so the browser asks the API's permission first."""

import pytest
from fastapi.testclient import TestClient

from services.api.main import app

ALLOWED = "http://localhost:3000"


@pytest.fixture
def client():
    return TestClient(app)


def preflight(client, origin, method="POST", headers="content-type"):
    return client.options(
        "/api/v1/missions",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def test_the_dashboard_may_create_a_mission(client):
    response = preflight(client, ALLOWED)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert "POST" in response.headers["access-control-allow-methods"]


def test_an_ordinary_request_from_the_dashboard_gets_the_allow_header(client):
    response = client.get("/health", headers={"Origin": ALLOWED})
    assert response.headers["access-control-allow-origin"] == ALLOWED


def test_another_website_is_not_allowed(client):
    assert preflight(client, "https://evil.example").status_code == 400
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_methods_the_dashboard_does_not_use_are_refused(client):
    assert preflight(client, ALLOWED, method="DELETE").status_code == 400


def test_cookies_are_never_allowed_across_origins(client):
    response = client.get("/health", headers={"Origin": ALLOWED})
    assert "access-control-allow-credentials" not in response.headers


def test_the_browser_may_read_the_location_header(client):
    response = client.get("/health", headers={"Origin": ALLOWED})
    assert "Location" in response.headers["access-control-expose-headers"]
