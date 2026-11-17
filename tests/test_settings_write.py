"""Запись настроек: точные команды на провод и поведение сущностей.

Все проверки на подставных ответах — реальные устройства и облако не затрагиваются.
"""
from __future__ import annotations

import json
from datetime import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

import custom_components.waviot_home.api as api_module
from custom_components.waviot_home.api import (
    CHARGER_CONFIRMATION_CMD,
    CHARGER_OBJECT_FIELDS,
    CHARGER_SCHEDULER_CMD,
    CHARGER_SETTINGS_CMD,
    WALLHUB_INDICATION_CMD,
    WaviotApiError,
    WaviotClient,
)
from custom_components.waviot_home.coordinator import WaviotLeakCoordinator
from custom_components.waviot_home.device import WaviotDeviceInfo
from custom_components.waviot_home.number import (
    PHOBOS_NUMBERS,
    WBOX_CONFIG_NUMBERS,
    WaviotChargerConfigNumber,
    WaviotPhobosNumber,
    WaviotPowersharingNumber,
)
from custom_components.waviot_home.parser import parse_device_status
from custom_components.waviot_home.pending import PENDING_TTL_SECONDS, PendingValue
from custom_components.waviot_home.select import (
    INDICATION_OPTIONS,
    WaviotIndicationSelect,
    indication_option,
)
from custom_components.waviot_home.switch import (
    WBOX_CONFIG_SWITCHES,
    WaviotChargerConfigSwitch,
    WaviotPowersharingSwitch,
)
from custom_components.waviot_home.time import (
    SCHEDULE_TIMES,
    WaviotScheduleTime,
    hhmm_to_time,
    time_to_minutes,
)

MODEM = "9"

SETTINGS = {
    "balance_enabled": 1, "case_temp_limit": 85, "charge_power_limit_enabled": 0,
    "charge_time_limit_enabled": 0, "hour_start_day_tariff": 4, "hour_start_night_tariff": 17,
    "max_balancing_power": 7680, "max_charge_power": 10000, "max_charge_time": 60,
    "use_two_tariff_enabled": 0,
}
SCHEDULER = {"start": False, "start_time": 1080, "stop": False, "stop_time": 480}
CONFIRMATION = {"charge_started": True, "nfc_accept": True}


# ======================= API: что уходит на провод =======================

class _Resp:
    def __init__(self, body, status=200):
        self.status = status
        self._body = body

    async def text(self):
        return json.dumps(self._body)

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _WireSession:
    """Отвечает на чтение настроек и статус команды; записывает всё, что POST-ится."""

    def __init__(self, status_body):
        self.status_body = status_body
        self.posts: list[dict] = []

    def get(self, url, headers=None, params=None):
        if url == api_module.STATUS_URL:
            return _Resp(self.status_body)
        return _Resp({MODEM: [{"db_id": "bd-1", "dl_status": "EXECUTED"}]})

    def post(self, url, headers=None, params=None, json=None):
        self.posts.append({"url": url, "params": params, "json": json})
        return _Resp({MODEM: {"bd_id": "bd-1"}})


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(api_module, "CHARGER_POLL_INTERVAL", 0)
    monkeypatch.setattr(api_module, "DOWNLINK_POLL_INTERVAL", 0)
    monkeypatch.setattr(api_module, "DOWNLINK_POLL_STEP", 0)


def _wire(status=None) -> tuple[WaviotClient, _WireSession]:
    body = {
        MODEM: status
        if status is not None
        else {
            CHARGER_SETTINGS_CMD: dict(SETTINGS),
            CHARGER_SCHEDULER_CMD: dict(SCHEDULER),
            CHARGER_CONFIRMATION_CMD: dict(CONFIRMATION),
        }
    }
    session = _WireSession(body)
    return WaviotClient(session), session


async def test_settings_write_sends_all_fields_with_only_one_changed():
    client, session = _wire()
    await client.charger_update_object(MODEM, "jwt", CHARGER_SETTINGS_CMD, {"max_charge_time": 90})

    [post] = session.posts
    assert post["params"] == {"modem_id": MODEM}
    assert post["json"] == {
        "cmd": "CHARGER_CMD_CHARGE_SETTINGS",
        "data": {**SETTINGS, "max_charge_time": 90},
    }
    assert set(post["json"]["data"]) == set(CHARGER_OBJECT_FIELDS[CHARGER_SETTINGS_CMD])


async def test_settings_write_keeps_types_of_untouched_fields():
    client, session = _wire()
    await client.charger_update_object(MODEM, "jwt", CHARGER_SETTINGS_CMD, {"balance_enabled": 0})
    data = session.posts[0]["json"]["data"]
    assert data["balance_enabled"] == 0 and isinstance(data["balance_enabled"], int)
    assert data["max_balancing_power"] == 7680


async def test_scheduler_write_keeps_booleans():
    client, session = _wire()
    await client.charger_update_object(MODEM, "jwt", CHARGER_SCHEDULER_CMD, {"start": True})
    assert session.posts[0]["json"] == {
        "cmd": "CHARGER_CMD_EVSE_SCHEDULER",
        "data": {"start": True, "start_time": 1080, "stop": False, "stop_time": 480},
    }


async def test_confirmation_write():
    client, session = _wire()
    await client.charger_update_object(MODEM, "jwt", CHARGER_CONFIRMATION_CMD, {"nfc_accept": False})
    assert session.posts[0]["json"]["data"] == {"charge_started": True, "nfc_accept": False}


@pytest.mark.parametrize(
    "status",
    [
        {},  # объекта настроек нет вообще
        {CHARGER_SETTINGS_CMD: {k: v for k, v in SETTINGS.items() if k != "max_charge_time"}},
        {CHARGER_SETTINGS_CMD: "garbage"},
    ],
    ids=["no-object", "missing-field", "not-a-dict"],
)
async def test_write_refused_when_current_settings_unavailable(status):
    """Выдуманный или частичный объект затёр бы настройки станции — не пишем ничего."""
    client, session = _wire(status)
    with pytest.raises(WaviotApiError):
        await client.charger_update_object(MODEM, "jwt", CHARGER_SETTINGS_CMD, {"max_charge_time": 90})
    assert session.posts == []


async def test_write_rejects_unknown_field_before_any_request():
    client, session = _wire()
    with pytest.raises(ValueError):
        await client.charger_update_object(MODEM, "jwt", CHARGER_SETTINGS_CMD, {"bogus": 1})
    assert session.posts == []


async def test_phobos_number_payload():
    client, session = _wire()
    await client.phobos_write_number(MODEM, "jwt", "0.0.17.0.0.255_3", 1500)
    assert session.posts[0]["json"] == {"cmd": "0.0.17.0.0.255_3", "data": 1500}


async def test_phobos_powersharing_payload_converts_kw_to_watts():
    client, session = _wire()
    await client.phobos_write_powersharing(MODEM, "jwt", True, 7.0)
    assert session.posts[0]["json"] == {
        "cmd": "powersharing",
        "data": {"state": True, "p_max": 7000.0},
    }


# ======================= PendingValue =======================

def test_pending_shows_written_value_until_cloud_changes():
    pending = PendingValue()
    assert pending.resolve("cloud") == "cloud"  # пока ничего не писали
    pending.set("new", baseline="old")
    assert pending.resolve("old") == "new"  # облако ещё не догнало
    assert pending.resolve("old") == "new"
    assert pending.resolve("new") == "new"  # облако изменилось — верим ему
    assert pending.resolve("old") == "old"  # и ожидание сброшено


def test_pending_expires(monkeypatch):
    import custom_components.waviot_home.pending as pending_module

    clock = {"t": 100.0}
    monkeypatch.setattr(pending_module.time, "monotonic", lambda: clock["t"])
    pending = PendingValue()
    pending.set("new", baseline="old")
    clock["t"] += PENDING_TTL_SECONDS + 1
    assert pending.resolve("old") == "old"


# ======================= сущности =======================

def _device(kind="wbox", hw_type="wbox_nvt_wa_stm") -> WaviotDeviceInfo:
    return WaviotDeviceInfo(modem_id=MODEM, hw_type=hw_type, kind=kind)


def _account() -> MagicMock:
    account = MagicMock()

    async def _call(func):
        return await func("jwt")

    account.async_call = AsyncMock(side_effect=_call)
    return account


def _coordinator(status: dict) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = {MODEM: status}
    return coordinator


def _client(**methods) -> MagicMock:
    client = MagicMock()
    for name, value in methods.items():
        setattr(client, name, value)
    return client


def _switch(spec_key: str, status: dict, client: MagicMock) -> WaviotChargerConfigSwitch:
    spec = next(s for s in WBOX_CONFIG_SWITCHES if s.key == spec_key)
    sw = WaviotChargerConfigSwitch(_coordinator(status), client, _account(), _device(), spec)
    sw.async_write_ha_state = MagicMock()
    return sw


async def test_settings_switch_writes_int_flag():
    client = _client(charger_update_object=AsyncMock())
    sw = _switch("balance_enabled", {"balance_enabled": False}, client)
    await sw.async_turn_on()
    client.charger_update_object.assert_awaited_once_with(
        MODEM, "jwt", CHARGER_SETTINGS_CMD, {"balance_enabled": 1}
    )


async def test_schedule_and_nfc_switches_write_booleans():
    client = _client(charger_update_object=AsyncMock())
    await _switch("schedule_start_enabled", {}, client).async_turn_on()
    client.charger_update_object.assert_awaited_with(
        MODEM, "jwt", CHARGER_SCHEDULER_CMD, {"start": True}
    )
    await _switch("nfc_confirmation", {}, client).async_turn_off()
    client.charger_update_object.assert_awaited_with(
        MODEM, "jwt", CHARGER_CONFIRMATION_CMD, {"nfc_accept": False}
    )


async def test_switch_shows_written_value_until_cloud_catches_up():
    client = _client(charger_update_object=AsyncMock())
    status = {"balance_enabled": False}
    sw = _switch("balance_enabled", status, client)
    assert sw.is_on is False
    await sw.async_turn_on()
    assert sw.is_on is True  # облако ещё отдаёт False
    status["balance_enabled"] = True  # облако догнало
    assert sw.is_on is True


async def test_switch_error_raises_and_does_not_pretend_success():
    client = _client(charger_update_object=AsyncMock(side_effect=WaviotApiError(0, "TIMEOUT")))
    sw = _switch("balance_enabled", {"balance_enabled": False}, client)
    with pytest.raises(HomeAssistantError):
        await sw.async_turn_on()
    assert sw.is_on is False


def test_switch_unavailable_without_status():
    spec = WBOX_CONFIG_SWITCHES[0]
    coordinator = MagicMock()
    coordinator.data = {}
    sw = WaviotChargerConfigSwitch(coordinator, _client(), _account(), _device(), spec)
    assert sw.is_on is None


def _number(spec_key: str, status: dict, client: MagicMock) -> WaviotChargerConfigNumber:
    spec = next(s for s in WBOX_CONFIG_NUMBERS if s.key == spec_key)
    num = WaviotChargerConfigNumber(_coordinator(status), client, _account(), _device(), spec)
    num.async_write_ha_state = MagicMock()
    return num


async def test_charger_number_writes_tariff_hour_unscaled():
    client = _client(charger_update_object=AsyncMock())
    await _number("hour_start_night_tariff", {}, client).async_set_native_value(22)
    client.charger_update_object.assert_awaited_once_with(
        MODEM, "jwt", CHARGER_SETTINGS_CMD, {"hour_start_night_tariff": 22}
    )


async def test_charger_number_error_raises():
    client = _client(charger_update_object=AsyncMock(side_effect=WaviotApiError(0, "x")))
    with pytest.raises(HomeAssistantError):
        await _number("hour_start_day_tariff", {}, client).async_set_native_value(6)


def test_charger_number_bounds_are_hours_of_day():
    for spec in WBOX_CONFIG_NUMBERS:
        assert (spec.minimum, spec.maximum) == (0, 23)


def _schedule_time(key: str, status: dict, client: MagicMock) -> WaviotScheduleTime:
    spec = next(s for s in SCHEDULE_TIMES if s.key == key)
    entity = WaviotScheduleTime(_coordinator(status), client, _account(), _device(), spec)
    entity.async_write_ha_state = MagicMock()
    return entity


async def test_schedule_time_written_as_minutes_from_midnight():
    client = _client(charger_update_object=AsyncMock())
    entity = _schedule_time("schedule_start_time", {"schedule_start_time": "18:00"}, client)
    assert entity.native_value == time(18, 0)
    await entity.async_set_value(time(7, 45))
    client.charger_update_object.assert_awaited_once_with(
        MODEM, "jwt", CHARGER_SCHEDULER_CMD, {"start_time": 465}
    )
    assert entity.native_value == time(7, 45)


def test_time_helpers():
    assert time_to_minutes(time(18, 0)) == 1080
    assert time_to_minutes(time(0, 0)) == 0
    assert time_to_minutes(time(23, 59)) == 1439
    assert hhmm_to_time("08:00") == time(8, 0)
    assert hhmm_to_time(None) is None
    assert hhmm_to_time("bad") is None


# --- Фобос: настройки счётчика ---

def _phobos_number(key: str, status: dict, client: MagicMock) -> WaviotPhobosNumber:
    spec = next(s for s in PHOBOS_NUMBERS if s.key == key)
    num = WaviotPhobosNumber(
        _coordinator(status), client, _account(), _device("phobos", "phobos3_wa_stm"), spec
    )
    num.async_write_ha_state = MagicMock()
    return num


async def test_phobos_number_writes_obis_command():
    client = _client(phobos_write_number=AsyncMock())
    await _phobos_number("power_limit", {}, client).async_set_native_value(4500)
    client.phobos_write_number.assert_awaited_once_with(MODEM, "jwt", "0.0.17.0.0.255_3", 4500)


async def test_instant_period_accepts_zero_or_at_least_ten_seconds():
    client = _client(phobos_write_number=AsyncMock())
    num = _phobos_number("instant_period", {}, client)
    await num.async_set_native_value(0)
    await num.async_set_native_value(10)
    assert client.phobos_write_number.await_count == 2
    with pytest.raises(HomeAssistantError):
        await num.async_set_native_value(5)
    assert client.phobos_write_number.await_count == 2  # недопустимое не ушло на провод


async def test_phobos_number_unknown_state_is_still_writable_and_shows_written_value():
    client = _client(phobos_write_number=AsyncMock())
    num = _phobos_number("time_turn_off", {"time_turn_off": None}, client)
    assert num.native_value is None
    await num.async_set_native_value(60)
    assert num.native_value == 60


def _powersharing(status: dict, client: MagicMock):
    device = _device("phobos", "phobos3_wa_stm")
    switch = WaviotPowersharingSwitch(_coordinator(status), client, _account(), device)
    number = WaviotPowersharingNumber(_coordinator(status), client, _account(), device)
    switch.async_write_ha_state = MagicMock()
    number.async_write_ha_state = MagicMock()
    return switch, number


async def test_powersharing_switch_sends_state_with_known_limit():
    client = _client(phobos_write_powersharing=AsyncMock())
    switch, _ = _powersharing({"powersharing_enabled": False, "powersharing_max": 7.0}, client)
    await switch.async_turn_on()
    client.phobos_write_powersharing.assert_awaited_once_with(MODEM, "jwt", True, 7.0)


async def test_powersharing_number_sends_limit_with_known_state():
    client = _client(phobos_write_powersharing=AsyncMock())
    _, number = _powersharing({"powersharing_enabled": True, "powersharing_max": 7.0}, client)
    await number.async_set_native_value(5.5)
    client.phobos_write_powersharing.assert_awaited_once_with(MODEM, "jwt", True, 5.5)


async def test_powersharing_refuses_to_guess_missing_half():
    """Команда несёт state и p_max вместе: недостающее не подставляем."""
    client = _client(phobos_write_powersharing=AsyncMock())
    switch, number = _powersharing({"powersharing_enabled": None, "powersharing_max": None}, client)
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()
    with pytest.raises(HomeAssistantError):
        await number.async_set_native_value(5.0)
    client.phobos_write_powersharing.assert_not_awaited()


def test_powersharing_number_bounds_match_mobile_app():
    _, number = _powersharing({}, _client())
    assert number.native_min_value == 0.1
    assert number.native_max_value == 249.9


# ======================= парсер: настройки Фобоса =======================

def test_phobos_settings_parsed_from_cloud_keys():
    raw = {
        "common": {"time_saved": __import__("time").time()},
        "1.0.0.8.9.255_2": 60,
        "0.0.17.0.0.255_3": 4500,
        "0.0.17.0.0.255_6": 30,
        "0.0.17.0.0.255_7": 120,
        "powersharing": {"state": True, "p_max": 7000},
    }
    status = parse_device_status("phobos", raw)
    assert status["instant_period"] == 60
    assert status["power_limit"] == 4500
    assert status["time_turn_off"] == 30
    assert status["time_turn_on"] == 120
    assert status["powersharing_enabled"] is True
    assert status["powersharing_max"] == 7.0


def test_phobos_settings_unknown_when_cloud_has_not_read_them():
    raw = {"common": {"time_saved": __import__("time").time()}}
    status = parse_device_status("phobos", raw)
    for key in ("instant_period", "power_limit", "time_turn_off", "time_turn_on",
                "powersharing_enabled", "powersharing_max"):
        assert status[key] is None


# ======================= согласованность спецификаций =======================

def test_all_control_keys_are_parsed_and_unique_unique_ids():
    wbox_raw = {
        "common": {"time_saved": __import__("time").time()},
        "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": {"state": 3},
    }
    wbox_status = parse_device_status("wbox", wbox_raw)
    keys = [s.key for s in WBOX_CONFIG_SWITCHES] + [s.key for s in WBOX_CONFIG_NUMBERS] + [
        s.key for s in SCHEDULE_TIMES
    ]
    assert len(keys) == len(set(keys))
    for key in keys:
        assert key in wbox_status, key

    phobos_status = parse_device_status(
        "phobos", {"common": {"time_saved": __import__("time").time()}}
    )
    for key in [s.key for s in PHOBOS_NUMBERS] + ["powersharing_enabled", "powersharing_max"]:
        assert key in phobos_status, key


def test_every_write_field_exists_in_the_object_the_app_writes():
    for spec in WBOX_CONFIG_SWITCHES:
        assert spec.field in CHARGER_OBJECT_FIELDS[spec.cmd], spec.key
    for spec in WBOX_CONFIG_NUMBERS:
        assert spec.field in CHARGER_OBJECT_FIELDS[CHARGER_SETTINGS_CMD], spec.key
    for spec in SCHEDULE_TIMES:
        assert spec.field in CHARGER_OBJECT_FIELDS[CHARGER_SCHEDULER_CMD], spec.key


def test_only_settings_present_in_the_mobile_app_are_writable():
    """Приложение не показывает и не меняет пределы мощности/времени и температуру корпуса —
    управлять ими из HA тоже не даём (и «максимум балансировки» только читаем: граница слайдера)."""
    not_in_app = {
        "charge_power_limit_enabled", "charge_time_limit_enabled", "max_charge_power",
        "max_charge_time", "case_temp_limit", "max_balancing_power",
    }
    written = (
        {s.field for s in WBOX_CONFIG_SWITCHES}
        | {s.field for s in WBOX_CONFIG_NUMBERS}
        | {s.field for s in SCHEDULE_TIMES}
    )
    assert not written & not_in_app


# ======================= имена и порядок =======================

def _config_names(switches, numbers, times) -> dict[str, str]:
    return (
        {s.key: s.name for s in switches}
        | {s.key: s.name for s in numbers}
        | {s.key: s.name for s in times}
    )


def test_ha_sorts_entities_by_name_so_switch_precedes_its_values_in_each_group():
    """HA сортирует сущности на странице устройства по алфавиту имён: переключатель
    группы должен идти первым, а число/время той же группы — сразу за ним, без чужих между."""
    names = _config_names(WBOX_CONFIG_SWITCHES, WBOX_CONFIG_NUMBERS, SCHEDULE_TIMES)
    groups = {
        "tariffs": ("use_two_tariff_enabled", ["hour_start_day_tariff", "hour_start_night_tariff"]),
        "start": ("schedule_start_enabled", ["schedule_start_time"]),
        "stop": ("schedule_stop_enabled", ["schedule_stop_time"]),
    }
    # NFC набран латиницей и в локали HA уходит вниз; в группе у него одна сущность.
    ordered = sorted((name, key) for key, name in names.items() if key != "nfc_confirmation")
    ordered_keys = [key for _, key in ordered]
    for switch_key, value_keys in groups.values():
        block = ordered_keys[ordered_keys.index(switch_key):][: 1 + len(value_keys)]
        assert block == [switch_key, *value_keys], (switch_key, block)


def test_schedule_names_read_as_start_then_stop():
    names = _config_names([], [], SCHEDULE_TIMES) | {
        s.key: s.name for s in WBOX_CONFIG_SWITCHES
    }
    assert names["schedule_start_enabled"] == "Расписание запуска: включено"
    assert names["schedule_start_time"] == "Расписание запуска: время"
    assert names["schedule_stop_enabled"] == "Расписание остановки: включено"
    assert names["schedule_stop_time"] == "Расписание остановки: время"


def test_two_tariffs_switch_is_followed_by_day_and_night_numbers_in_same_group():
    names = {s.key: s.name for s in WBOX_CONFIG_SWITCHES} | {s.key: s.name for s in WBOX_CONFIG_NUMBERS}
    assert names["use_two_tariff_enabled"] == "Тарифы: два тарифа"
    assert names["hour_start_day_tariff"].startswith("Тарифы:")
    assert names["hour_start_night_tariff"].startswith("Тарифы:")


def test_phobos_control_names_are_grouped():
    assert [s.name for s in PHOBOS_NUMBERS] == [
        "Мгновенные данные: период отправки",
        "Ограничение мощности: порог отключения",
        "Ограничение мощности: время отключения",
        "Ограничение мощности: время включения",
    ]
    _, number = _powersharing({}, _client())
    assert number.name == "Балансировка: лимит"


def test_no_sensor_or_binary_sensor_duplicates_a_control():
    """Настройка, у которой есть переключатель/число/время, не повторяется сенсором."""
    from custom_components.waviot_home.binary_sensor import WBOX_BINARY_SENSORS
    from custom_components.waviot_home.sensor import WBOX_SENSORS

    control_keys = (
        {s.key for s in WBOX_CONFIG_SWITCHES}
        | {s.key for s in WBOX_CONFIG_NUMBERS}
        | {s.key for s in SCHEDULE_TIMES}
    )
    read_only = {s.key for s in WBOX_SENSORS} | {s.key for s in WBOX_BINARY_SENSORS}
    assert not control_keys & read_only


# ======================= СмартХаб: длительность индикации =======================

def _indication(status: dict, client: MagicMock) -> WaviotIndicationSelect:
    device = _device("wallhub", "wallhub_stm")
    select = WaviotIndicationSelect(_coordinator(status), client, _account(), device)
    select.async_write_ha_state = MagicMock()
    return select


def test_indication_options_are_the_four_from_the_mobile_app():
    assert list(INDICATION_OPTIONS) == ["Никогда", "30 с", "5 минут", "Всегда"]


def test_indication_option_from_hub_settings():
    assert indication_option(0, False) == "Никогда"
    assert indication_option(30, False) == "30 с"
    assert indication_option(300, False) == "5 минут"
    assert indication_option(30, True) == "Всегда"  # infinite важнее timeout
    assert indication_option(45, False) is None  # нестандартное значение — неизвестно
    assert indication_option(None, None) is None


async def test_indication_select_sends_app_payloads():
    client = _client(wallhub_write_indication=AsyncMock())
    select = _indication({"indication_timeout": 30, "indication_infinite": False}, client)
    for option, data in {
        "Никогда": {"timeout": 0},
        "30 с": {"timeout": 30},
        "5 минут": {"timeout": 300},
        "Всегда": {"infinite": True},
    }.items():
        client.wallhub_write_indication.reset_mock()
        await select.async_select_option(option)
        client.wallhub_write_indication.assert_awaited_once_with(MODEM, "jwt", data)


async def test_indication_select_shows_written_value_until_cloud_catches_up():
    client = _client(wallhub_write_indication=AsyncMock())
    status = {"indication_timeout": 30, "indication_infinite": False}
    select = _indication(status, client)
    assert select.current_option == "30 с"
    await select.async_select_option("Всегда")
    assert select.current_option == "Всегда"
    status.update(indication_infinite=True)  # облако догнало
    assert select.current_option == "Всегда"


async def test_indication_select_error_raises_and_keeps_state():
    client = _client(wallhub_write_indication=AsyncMock(side_effect=WaviotApiError(0, "x")))
    select = _indication({"indication_timeout": 30, "indication_infinite": False}, client)
    with pytest.raises(HomeAssistantError):
        await select.async_select_option("Никогда")
    assert select.current_option == "30 с"


async def test_wallhub_api_payload_on_the_wire():
    client, session = _wire({})
    await client.wallhub_write_indication(MODEM, "jwt", {"infinite": True})
    assert session.posts[0]["json"] == {"cmd": WALLHUB_INDICATION_CMD, "data": {"infinite": True}}
    assert WALLHUB_INDICATION_CMD == "SSERIAL_CMD_INDICATION_TIMEOUT"


# ======================= датчик протечки: координатор и сенсоры =======================

def _leak_record(name: str, ts: float = 1000.0) -> dict:
    return {"timestamp": ts, "data": {"0": {"name": name, "value": 1}}}


async def _leak_update(records: dict) -> dict:
    async def _call(func):
        return await func("jwt")

    account = MagicMock()
    account.async_call = AsyncMock(side_effect=_call)
    client = MagicMock()

    async def _get(modem_id, jwt):
        return records[modem_id]

    client.get_last_leak_event = _get
    fake = SimpleNamespace(_account=account, _client=client, _modem_ids=list(records))
    return await WaviotLeakCoordinator._async_update_data(fake)


async def test_leak_coordinator_flags_leak_only_for_last_event_detected():
    data = await _leak_update(
        {
            "1": _leak_record("leakage_detected"),
            "2": _leak_record("leakage_fixed"),
            "3": _leak_record("test_button_pressed"),
            "4": _leak_record("reset"),
            "5": None,  # событий ещё не было
        }
    )
    assert data == {"1": True, "2": False, "3": False, "4": False, "5": False}


# ======================= сверка с приложением =======================

def test_hub_entities_are_only_what_the_app_shows():
    from custom_components.waviot_home.sensor import WALLHUB_SENSORS

    assert [s.key for s in WALLHUB_SENSORS] == ["sw_version"]


def test_ladoga_has_no_extra_sensors_besides_battery():
    import custom_components.waviot_home.sensor as sensor_module

    assert not hasattr(sensor_module, "LADOGA_SENSORS")
    assert not hasattr(sensor_module, "WaviotLeakStateSensor")
    assert not hasattr(sensor_module, "WaviotLeakEventTimeSensor")


def test_no_diagnostics_the_app_does_not_show_remain_in_sensor_tables():
    from custom_components.waviot_home.sensor import PHOBOS_SENSORS, WBOX_SENSORS

    forbidden = {"rssi", "snr", "board_temp", "hw_version", "wa1470_rssi", "lte_rssi", "wifi_rssi",
                 "sw_version_1", "sw_version_2", "hw_version_1", "hw_version_2", "esp_fw",
                 "nvt_fw", "case_temp_limit", "last_card"}
    keys = {s.key for s in PHOBOS_SENSORS} | {s.key for s in WBOX_SENSORS}
    assert not keys & forbidden


def test_last_contact_is_not_exposed_anywhere():
    """«Последний контакт» убран у всех приборов; time_saved остался только для офлайн-проверки."""
    from custom_components.waviot_home.sensor import (
        PHOBOS_SENSORS,
        WALLHUB_SENSORS,
        WBOX_SENSORS,
    )

    keys = {s.key for s in PHOBOS_SENSORS} | {s.key for s in WBOX_SENSORS} | {
        s.key for s in WALLHUB_SENSORS
    }
    assert "last_contact" not in keys
