"""Тесты WaviotClient — команды зарядки wbox (единицы измерения критичны)."""
from __future__ import annotations

import json

import pytest

import custom_components.waviot_home.api as api_module
from custom_components.waviot_home.api import WaviotClient


class _FakeResponse:
    def __init__(self, status: int, json_body):
        self.status = status
        self._json_body = json_body

    async def text(self):
        return json.dumps(self._json_body)

    async def json(self, content_type=None):
        return self._json_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Мок aiohttp.ClientSession, фиксирующий вызовы для проверки реального payload."""

    def __init__(self):
        self.post_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def post(self, url, headers=None, params=None, json=None):
        self.post_calls.append({"url": url, "headers": headers, "params": params, "json": json})
        return _FakeResponse(200, {params["modem_id"]: {"bd_id": "bd-1"}})

    def get(self, url, headers=None, params=None):
        self.get_calls.append({"url": url, "headers": headers, "params": params})
        return _FakeResponse(
            200, {params["modem_id"]: [{"db_id": "bd-1", "dl_status": "EXECUTED"}]}
        )


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Команды зарядки спят 2 сек между попытками — не ждать этого в тестах."""
    monkeypatch.setattr(api_module, "CHARGER_POLL_INTERVAL", 0)


@pytest.fixture
def session() -> _FakeSession:
    return _FakeSession()


@pytest.fixture
def client(session) -> WaviotClient:
    return WaviotClient(session)


async def test_charger_set_max_power_converts_kw_to_watts(client, session):
    """При ЗАПИСИ API ждёт Вт, хотя при ЧТЕНИИ (parser.py) те же поля приходят
    в кВт — несимметрично, но это фактическое поведение API."""
    await client.charger_set_max_power("123", "jwt", 7.5)

    assert len(session.post_calls) == 1
    body = session.post_calls[0]["json"]
    assert body["cmd"] == "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD"
    assert body["data"]["action"] == 0
    assert body["data"]["max_power"] == 7500  # 7.5 кВт -> 7500 Вт, не 7.5!


async def test_charger_start_stop_sends_correct_actions(client, session):
    await client.charger_start_stop("123", "jwt", True)
    assert session.post_calls[0]["json"]["data"]["action"] == 2  # start

    await client.charger_start_stop("123", "jwt", False)
    assert session.post_calls[1]["json"]["data"]["action"] == 4  # stop


async def test_charger_command_raises_on_failed_status(client, session):
    from custom_components.waviot_home.api import WaviotApiError

    session.get = lambda url, headers=None, params=None: _FakeResponse(
        200, {params["modem_id"]: [{"db_id": "bd-1", "dl_status": "FAILED"}]}
    )

    with pytest.raises(WaviotApiError):
        await client.charger_start_stop("123", "jwt", True)
