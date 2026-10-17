import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from mtcontroller.config import Settings, WebConfig  # noqa: E402
from mtcontroller.web.app import create_app  # noqa: E402


@pytest.fixture
def client():
    s = Settings(web=WebConfig(ui_host="admin.test", checkin_host="checkin.test"))
    app = create_app(s)

    @app.get("/api/admin/ping")
    async def admin_ping():
        return {"ok": True}

    @app.post("/api/checkin")
    async def checkin():
        return {"ok": True}

    return TestClient(app)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.headers["x-frame-options"] == "DENY"


def test_admin_only_on_admin_host(client):
    assert client.get("/api/admin/ping", headers={"host": "admin.test"}).status_code == 200
    assert client.get("/api/admin/ping", headers={"host": "checkin.test"}).status_code == 404


def test_checkin_only_on_checkin_host(client):
    assert client.post("/api/checkin", headers={"host": "checkin.test"}).status_code == 200
    assert client.post("/api/checkin", headers={"host": "admin.test"}).status_code == 404


def test_no_public_api_docs(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
