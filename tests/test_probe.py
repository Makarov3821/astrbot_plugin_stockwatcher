import asyncio

from stockwatcher.probe import run


def test_probe_without_credentials_is_not_reported_verified(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    result, status = asyncio.run(run("MU"))
    assert status == 2
    assert result["status"] == "not_verified"
    assert result["code"] == "credentials_missing"
