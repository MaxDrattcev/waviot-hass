"""Тесты parse_device_status — без реального HA, только чистая логика."""
import time

from custom_components.waviot.const import (
    HW_TYPE_AQUA,
    HW_TYPE_BETAR,
    HW_TYPE_GEFEST,
    HW_TYPE_LADOGA,
    HW_TYPE_PHOBOS,
    HW_TYPE_WALLHUB,
    HW_TYPE_WBOX,
    detect_hw_type,
)
from custom_components.waviot.parser import parse_device_status


def _online_common() -> dict:
    return {"time_saved": int(time.time())}


def _offline_common() -> dict:
    return {"time_saved": int(time.time()) - 86400 * 3}


def test_detect_hw_type_case_insensitive_substring():
    assert detect_hw_type("Waviot-Phobos-v2") == HW_TYPE_PHOBOS
    assert detect_hw_type("AQUA_METER") == HW_TYPE_AQUA
    assert detect_hw_type("unknown-device") is None


def test_phobos_online_parses_relay_and_meter():
    raw = {
        "common": _online_common(),
        "0.0.96.3.10.255_2": 1,
        "0100010800FF": 123.45,
        "0100200700FF": 230.0,
        "0100150700FF": 500.0,
        "01001F0700FF": 2.5,
    }
    status = parse_device_status(HW_TYPE_PHOBOS, raw)
    assert status == {
        "on": True,
        "meter": 123.45,
        "voltage": 230.0,
        "power": 500.0,
        "amperage": 2.5,
    }


def test_phobos_offline_returns_none():
    raw = {"common": _offline_common(), "0.0.96.3.10.255_2": 1}
    assert parse_device_status(HW_TYPE_PHOBOS, raw) is None


def test_phobos_no_common_returns_none():
    assert parse_device_status(HW_TYPE_PHOBOS, {}) is None


def test_aqua_parses_liters_and_heartbeat_voltage():
    raw = {
        "common": _online_common(),
        "liter_forw_VAL": 1000.0,
        "heartbeat": {"voltage": 3.6},
    }
    status = parse_device_status(HW_TYPE_AQUA, raw)
    assert status == {"meter": 1000.0, "voltage": 3.6}


def test_gefest_divides_full_1_by_1000():
    raw = {"common": _online_common(), "full_1": 5000.0, "heartbeat": {"voltage": 3.5}}
    status = parse_device_status(HW_TYPE_GEFEST, raw)
    assert status == {"meter": 5.0, "voltage": 3.5}


def test_betar_has_no_voltage_from_batch():
    """voltage у betar добавляется отдельно координатором (GetVoltBetar), не отсюда."""
    raw = {"common": _online_common(), "full_1": 2000.0}
    status = parse_device_status(HW_TYPE_BETAR, raw)
    assert status == {"meter": 2.0}


def test_ladoga_online_returns_voltage_only():
    raw = {"common": _online_common(), "heartbeat": {"voltage": 3.3}}
    status = parse_device_status(HW_TYPE_LADOGA, raw)
    assert status == {"voltage": 3.3}


def test_wallhub_parses_temperature_and_humidity():
    raw = {
        "common": _online_common(),
        "SSERIAL_CMD_TEMPERATURE": {"temperature": 21.5, "humidity": 45.0},
    }
    status = parse_device_status(HW_TYPE_WALLHUB, raw)
    assert status == {"temperature": 21.5, "humidity": 45.0}


def test_unknown_kind_returns_none():
    assert parse_device_status("unknown", {"common": _online_common()}) is None


def _wbox_short_cmd(state: int, **overrides) -> dict:
    data = {
        "state": state,
        "inst_power": 3.5,
        "max_power": 7.0,
        "max_power_limit": 11.0,
        "count_power": 2.1,
    }
    data.update(overrides)
    return data


def test_wbox_charging_parses_full_status():
    raw = {
        "common": _online_common(),
        "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(4),  # Charging
        "CHARGER_CMD_GET_TEMPERATURE": {"temperature_avg": 32.5},
    }
    status = parse_device_status(HW_TYPE_WBOX, raw)
    assert status == {
        "state": 4,
        "on": True,
        "power": 3.5,
        "max_power": 7.0,
        "max_power_limit": 11.0,
        "meter": 2.1,
        "temperature": 32.5,
    }


def test_wbox_waiting_start_is_not_on():
    raw = {
        "common": _online_common(),
        "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(3),  # WaitingStart
    }
    status = parse_device_status(HW_TYPE_WBOX, raw)
    assert status["state"] == 3
    assert status["on"] is False


def test_wbox_ready_to_start_is_not_on():
    raw = {
        "common": _online_common(),
        "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(1),  # ReadyToStart, разъём свободен
    }
    status = parse_device_status(HW_TYPE_WBOX, raw)
    assert status["state"] == 1
    assert status["on"] is False


def test_wbox_missing_short_cmd_returns_none():
    raw = {"common": _online_common()}
    assert parse_device_status(HW_TYPE_WBOX, raw) is None


def test_wbox_offline_rssi_50_uses_20min_threshold():
    common = {"time_saved": int(time.time()) - 21 * 60, "rssi": 50}
    raw = {"common": common, "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(4)}
    assert parse_device_status(HW_TYPE_WBOX, raw) is None


def test_wbox_online_rssi_50_within_20min_threshold():
    common = {"time_saved": int(time.time()) - 10 * 60, "rssi": 50}
    raw = {"common": common, "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(4)}
    assert parse_device_status(HW_TYPE_WBOX, raw) is not None


def test_wbox_offline_default_uses_120min_threshold():
    common = {"time_saved": int(time.time()) - 121 * 60}  # rssi отсутствует/не 50
    raw = {"common": common, "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(4)}
    assert parse_device_status(HW_TYPE_WBOX, raw) is None


def test_wbox_online_default_within_120min_threshold():
    common = {"time_saved": int(time.time()) - 60 * 60}
    raw = {"common": common, "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(4)}
    assert parse_device_status(HW_TYPE_WBOX, raw) is not None


def test_detect_hw_type_wbox():
    assert detect_hw_type("wbox_nvt_wa_stm") == HW_TYPE_WBOX


def test_raw_none_returns_none():
    assert parse_device_status(HW_TYPE_PHOBOS, None) is None
