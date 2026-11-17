"""История зарядок: разбор ответа облака, событие календаря, фильтр периода, кэш и сбои."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

import custom_components.waviot_home.calendar as calendar_module
from custom_components.waviot_home.api import WaviotApiError, WaviotClient
from custom_components.waviot_home.calendar import (
    WaviotChargeHistoryCalendar,
    session_to_event,
)
from custom_components.waviot_home.const import DOMAIN
from custom_components.waviot_home.device import WaviotDeviceInfo
from custom_components.waviot_home.parser import parse_charge_history

END = 1794231860  # время окончания записи со станции, снятой read-only 12.11.2026


def _record(end=END, duration=2302, t1=2.121, t2=0.0) -> dict:
    return {
        "timestamp": end,
        "data": {"data": {"0100000809FF": duration, "0100010801FF": t1, "0100010802FF": t2}},
    }


def _calendar(records=None, error=None) -> tuple[WaviotChargeHistoryCalendar, MagicMock]:
    client = MagicMock(spec=WaviotClient)
    client.get_charge_history = AsyncMock(return_value=records or [], side_effect=error)
    account = MagicMock()

    async def _async_call(func):
        return await func("jwt")

    account.async_call = AsyncMock(side_effect=_async_call)
    device = WaviotDeviceInfo(modem_id="9", hw_type="wbox_nvt_wa_stm", kind="wbox")
    return WaviotChargeHistoryCalendar(client, account, device), client


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


# --- разбор ---------------------------------------------------------------------------

def test_parse_history_sorted_ascending_and_skips_broken_records():
    records = [_record(end=300), {"timestamp": 1}, _record(end=100), "junk", _record(end=200)]
    assert [s["end"] for s in parse_charge_history(records)] == [100, 200, 300]


def test_parse_history_handles_non_list():
    assert parse_charge_history(None) == []
    assert parse_charge_history({}) == []


def test_parse_history_values():
    [session] = parse_charge_history([_record(t1=1.5, t2=0.5)])
    assert session == {"end": END, "duration": 2302, "t1": 1.5, "t2": 0.5}


# --- событие --------------------------------------------------------------------------

def test_event_start_is_end_minus_duration_and_summary_has_total_energy():
    [session] = parse_charge_history([_record(t1=1.5, t2=0.5)])
    event = session_to_event("9", session)
    assert event.end == _dt(END)
    assert event.start == _dt(END) - timedelta(seconds=2302)
    assert event.summary == "Зарядка 2,00 кВт·ч"
    assert event.uid == f"9_{END}"
    assert "Тариф 2: 0,50 кВт·ч" in event.description
    assert "Длительность: 38 мин" in event.description


def test_event_hides_second_tariff_when_zero_and_formats_hours():
    [session] = parse_charge_history([_record(duration=3 * 3600 + 5 * 60, t2=0.0)])
    event = session_to_event("9", session)
    assert "Тариф 2" not in event.description
    assert "Длительность: 3 ч 5 мин" in event.description


# --- календарь ------------------------------------------------------------------------

async def test_get_events_returns_only_sessions_overlapping_period():
    cal, _ = _calendar([_record(end=END), _record(end=END + 10 * 86400)])
    events = await cal.async_get_events(MagicMock(), _dt(END - 3600), _dt(END + 3600))
    assert [e.uid for e in events] == [f"9_{END}"]


async def test_get_events_includes_session_that_started_before_period():
    cal, _ = _calendar([_record(end=END, duration=7200)])
    events = await cal.async_get_events(MagicMock(), _dt(END - 60), _dt(END + 60))
    assert len(events) == 1


async def test_history_cached_between_calls_and_refetched_after_ttl(monkeypatch):
    cal, client = _calendar([_record()])
    clock = {"t": 1000.0}
    monkeypatch.setattr(calendar_module.time, "monotonic", lambda: clock["t"])

    await cal.async_get_events(MagicMock(), _dt(0), _dt(END * 2))
    await cal.async_get_events(MagicMock(), _dt(0), _dt(END * 2))
    assert client.get_charge_history.await_count == 1

    clock["t"] += calendar_module.HISTORY_CACHE_SECONDS + 1
    await cal.async_get_events(MagicMock(), _dt(0), _dt(END * 2))
    assert client.get_charge_history.await_count == 2


@pytest.mark.parametrize(
    "error",
    [WaviotApiError(500, "boom"), aiohttp.ClientConnectionError("down"), TimeoutError()],
    ids=lambda e: type(e).__name__,
)
async def test_failure_returns_empty_without_cache_and_old_data_with_cache(error, monkeypatch):
    cal, client = _calendar([_record()])
    clock = {"t": 1000.0}
    monkeypatch.setattr(calendar_module.time, "monotonic", lambda: clock["t"])
    assert await cal.async_get_events(MagicMock(), _dt(0), _dt(END * 2))  # загрузили

    client.get_charge_history.side_effect = error
    clock["t"] += calendar_module.HISTORY_CACHE_SECONDS + 1
    assert len(await cal.async_get_events(MagicMock(), _dt(0), _dt(END * 2))) == 1

    fresh, _ = _calendar(error=error)
    assert await fresh.async_get_events(MagicMock(), _dt(0), _dt(END * 2)) == []


def test_calendar_identity_and_state():
    cal, _ = _calendar()
    assert cal.unique_id == "9_charge_history"
    assert cal.name == "История зарядок"
    assert cal.has_entity_name is True
    assert cal.device_info["identifiers"] == {(DOMAIN, "9")}
    assert cal.event is None


# --- API ------------------------------------------------------------------------------

class _Resp:
    status = 200

    def __init__(self, body):
        self._body = body

    async def text(self):
        return "ok"

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, body):
        self._body = body
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append({"url": url, "params": params})
        return _Resp(self._body)


async def test_api_requests_history_like_mobile_app_without_limit_or_dates():
    session = _Session({"9": [_record()]})
    records = await WaviotClient(session).get_charge_history("9", "jwt")

    assert records == [_record()]
    assert session.calls[0]["url"].endswith("/api/roll")
    assert session.calls[0]["params"] == {
        "modem_id": "9",
        "tag": "electro5.data_profile_charge",
    }


async def test_api_history_empty_for_unexpected_payload():
    assert await WaviotClient(_Session({})).get_charge_history("9", "jwt") == []
    assert await WaviotClient(_Session([])).get_charge_history("9", "jwt") == []
