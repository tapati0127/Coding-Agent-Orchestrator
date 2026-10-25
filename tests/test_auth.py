import pytest
from langgraph_sdk import Auth
from orchestrator.auth import authenticate
from orchestrator.config import Settings


async def test_runtime_key_auth_and_dashboard_denied(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "runtime-secret")
    monkeypatch.setenv("DASHBOARD_TOKEN", "dashboard-secret")
    assert (await authenticate({b"x-api-key": b"runtime-secret"}))["identity"] == "orchestrator-runtime"
    assert (await authenticate(authorization="Bearer runtime-secret"))["permissions"] == ["runtime"]
    with pytest.raises(Auth.exceptions.HTTPException) as error:
        await authenticate(authorization="Bearer dashboard-secret")
    assert error.value.status_code == 401


async def test_unconfigured_auth_fails_closed(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_TOKEN", raising=False)
    with pytest.raises(Auth.exceptions.HTTPException) as error:
        await authenticate()
    assert error.value.status_code == 503


async def test_shared_tokens_fails_closed(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_TOKEN", "same")
    monkeypatch.setenv("DASHBOARD_TOKEN", "same")
    with pytest.raises(Auth.exceptions.HTTPException) as error:
        await authenticate(authorization="Bearer same")
    assert error.value.status_code == 503


def test_settings_validate_limits_and_hide_secrets():
    for field in ["MAX_EXECUTE_WORKERS", "MAX_QWEN_SESSIONS"]:
        with pytest.raises(ValueError):
            Settings.from_env({field: "6"})
    with pytest.raises(ValueError):
        Settings.from_env({"MAX_BACKWARD_DELEGATIONS": "4"})
    with pytest.raises(ValueError):
        Settings.from_env({"DISPATCH_POLL_SECONDS": "0"})
    with pytest.raises(ValueError):
        Settings.from_env({"HUMAN_RESPONSE_TIMEOUT_SECONDS": "0"})
    assert Settings.from_env({"HUMAN_RESPONSE_TIMEOUT_SECONDS": "1800"}).human_response_timeout_seconds == 1800
    settings = Settings.from_env({"QWEN_API_KEY": "private", "ORCHESTRATOR_TOKEN": "private2"})
    assert "private" not in repr(settings)
