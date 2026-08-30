import asyncio

from httpx import ASGITransport, AsyncClient

from app.main import app


async def request(method: str, url: str, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, url, **kwargs)


def test_health() -> None:
    assert asyncio.run(request("GET", "/api/health")).json() == {"status": "ok"}


def test_create_session_and_register_device() -> None:
    response = asyncio.run(request("POST", "/api/sessions", json={"name": "Test"}))
    assert response.status_code == 201
    session = response.json()
    assert session["schema_version"] == "1.0"

    response = asyncio.run(request(
        "POST",
        f"/api/sessions/{session['session_id']}/devices",
        json={"name": "Kamera 1", "role": "main_camera", "capabilities": {"battery_percent": 87}},
    ))
    assert response.status_code == 201
    assert response.json()["state"] == "ready"

    response = asyncio.run(request("GET", f"/api/sessions/{session['session_id']}"))
    assert len(response.json()["devices"]) == 1

    response = asyncio.run(request("GET", "/api/sessions/current"))
    assert response.json()["session_id"] == session["session_id"]

    for number in (1, 2):
        response = asyncio.run(request(
            "POST",
            f"/api/sessions/{session['session_id']}/devices",
            json={"name": f"Vedlejší {number}", "role": "secondary_camera"},
        ))
        assert response.status_code == 201

    response = asyncio.run(request("GET", f"/api/sessions/{session['session_id']}"))
    secondary = [device for device in response.json()["devices"].values() if device["role"] == "secondary_camera"]
    assert len(secondary) == 2


def test_unknown_session_is_404() -> None:
    response = asyncio.run(request("GET", "/api/sessions/00000000-0000-0000-0000-000000000000"))
    assert response.status_code == 404
