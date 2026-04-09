from fastapi.testclient import TestClient

from app.api.app import app


client = TestClient(app)


def test_broker_status_route() -> None:
    response = client.get("/broker/status")
    assert response.status_code == 200
    data = response.json()
    assert data["broker_mode"] in {"paper", "mock", "alpaca"}


def test_broker_account_route() -> None:
    response = client.get("/broker/account")
    assert response.status_code == 200
    data = response.json()
    assert data["cash"] == 100000.0


def test_auto_status_route() -> None:
    response = client.get("/auto/status")
    assert response.status_code == 200
    data = response.json()
    assert "running" in data


def test_auto_start_stop_routes() -> None:
    start_response = client.post("/auto/start")
    assert start_response.status_code == 200
    assert "message" in start_response.json()

    stop_response = client.post("/auto/stop")
    assert stop_response.status_code == 200
    assert "message" in stop_response.json()


def test_auto_run_now_route() -> None:
    response = client.post("/auto/run-now")
    assert response.status_code == 200
    data = response.json()
    assert "success" in data
