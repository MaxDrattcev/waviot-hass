"""Тесты parse_device_status — без реального HA, только чистая логика."""
import time

from custom_components.waviot_home.const import (
    HW_TYPE_AQUA,
    HW_TYPE_BETAR,
    HW_TYPE_GEFEST,
    HW_TYPE_LADOGA,
    HW_TYPE_PHOBOS,
    HW_TYPE_WALLHUB,
    HW_TYPE_WBOX,
    detect_hw_type,
)
from custom_components.waviot_home.parser import (
    apply_instant,
    parse_charge_session,
    parse_device_status,
    parse_leak_event,
)


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
    assert status["on"] is True
    assert status["meter"] == 123.45
    assert status["voltage"] == 230.0
    assert status["power"] == 500.0
    assert status["amperage"] == 2.5


def test_phobos_missing_instant_values_are_none_not_zero():
    """Счётчик без мгновенного профиля: «нет данных», а не ложные 0 В / 0 Вт / 0 А."""
    raw = {"common": _online_common(), "0.0.96.3.10.255_2": 1, "0100010800FF": 10}
    status = parse_device_status(HW_TYPE_PHOBOS, raw)
    assert status["voltage"] is None
    assert status["power"] is None
    assert status["amperage"] is None


def test_phobos_parses_tariffs_and_app_info_fields():
    common = {**_online_common(), "rssi": -101, "snr": 47}
    raw = {
        "common": common,
        "0100010800FF": 1000,
        "0100010801FF": 100,
        "0100010802FF": 200,
        "0100010803FF": 300,
        "0100010804FF": 400,
        "heartbeat": {"temp": 35, "voltage": 3.31},
        "version": {"sw": "3.4.8.7", "hw": "3.4.5.0"},
    }
    status = parse_device_status(HW_TYPE_PHOBOS, raw)
    assert [status[f"energy_t{i}"] for i in range(1, 5)] == [100, 200, 300, 400]
    assert status["battery_voltage"] == 3.31
    assert status["sw_version"] == "3.4.8.7"
    assert "last_contact" not in status


def test_phobos_has_no_fields_the_mobile_app_does_not_show():
    raw = {"common": {**_online_common(), "rssi": -90, "snr": 40}, "heartbeat": {"temp": 30},
           "version": {"hw": "1"}}
    status = parse_device_status(HW_TYPE_PHOBOS, raw)
    for key in ("rssi", "snr", "board_temp", "hw_version"):
        assert key not in status


def test_apply_instant_adds_phases_and_overrides_settings_values():
    raw = {"common": _online_common(), "0100200700FF": 1.0}
    status = parse_device_status(HW_TYPE_PHOBOS, raw)
    apply_instant(
        status,
        {
            "32.7.0": 230.1, "52.7.0": 231.2, "72.7.0": 229.3,
            "31.7.0": 1.5, "21.7.0": 340.0, "1.7.0": 900.0,
            "14.7.0": 50.0, "91.7.0": 0.2, "ts_get": 1,
        },
    )
    assert status["voltage"] == 230.1  # мгновенный профиль приоритетнее настроек
    assert status["voltage_b"] == 231.2
    assert status["voltage_c"] == 229.3
    assert status["amperage"] == 1.5
    assert status["power_sum"] == 900.0
    assert status["frequency"] == 50.0
    assert status["neutral_current"] == 0.2
    assert "power_b" not in status  # поля, которых нет в записи, не выдумываем


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


def test_betar_meter_and_voltage_come_from_eav_not_from_batch():
    """Показание и напряжение бетара берёт координатор из EAV (как приложение): `full_1` в
    другом масштабе (у реального прибора 187795 против 1877.95 м³ в приложении)."""
    raw = {"common": _online_common(), "full_1": 187795.0, "div_1": 1}
    status = parse_device_status(HW_TYPE_BETAR, raw)
    assert status == {"meter": None, "voltage": None}


def test_ladoga_online_returns_voltage_only():
    raw = {"common": _online_common(), "heartbeat": {"voltage": 3.3}}
    status = parse_device_status(HW_TYPE_LADOGA, raw)
    assert status == {"voltage": 3.3}


def _wallhub_raw(**extra) -> dict:
    """Ответ реального СмартХаба (12391944), без секретов."""
    return {
        "common": _online_common(),
        "SSERIAL_CMD_TEMPERATURE": {"temperature": 21.5, "humidity": 45.0},
        "CHARGER_DEVICE_EVSE_ESP_FIRMWARE": {
            "hw_ver_id": 1, "hw_ver_major": 2, "hw_ver_minor": 6, "progress": 0, "state": 0,
        },
        "SSERIAL_CMD_INDICATION_TIMEOUT": {"infinite": False, "timeout": 30},
        **extra,
    }


def test_wallhub_parses_temperature_humidity_and_info():
    raw = _wallhub_raw()
    status = parse_device_status(HW_TYPE_WALLHUB, raw)
    assert status["temperature"] == 21.5
    assert status["humidity"] == 45.0
    assert status["sw_version"] == "1.2.6"  # версия прошивки ESP, формат приложения id.major.minor
    assert "last_contact" not in status
    assert status["indication_timeout"] == 30
    assert status["indication_infinite"] is False


def test_wallhub_missing_values_are_none_not_zero():
    raw = {"common": _online_common()}
    status = parse_device_status(HW_TYPE_WALLHUB, raw)
    for key in ("temperature", "humidity", "sw_version", "indication_timeout",
                "indication_infinite"):
        assert status[key] is None


def test_firmware_version_matches_mobile_app_format():
    """Приложение: Version(hwVerId, hwVerMajor, hwVerMinor) -> «id.major.minor»."""
    raw = {"CHARGER_DEVICE_EVSE_ESP_FIRMWARE": {"hw_ver_id": 1, "hw_ver_major": 2, "hw_ver_minor": 2},
           "CHARGER_DEVICE_EVSE_NVT_FIRMWARE": {"hw_ver_id": 2, "hw_ver_major": 2, "hw_ver_minor": 3}}
    raw_wbox = {"common": _online_common(), "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(3), **raw}
    assert parse_device_status(HW_TYPE_WBOX, raw_wbox)["sw_version"] == "1.2.2, 2.2.3"


def test_firmware_version_none_when_incomplete():
    raw = {"common": _online_common(), "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(3),
           "CHARGER_DEVICE_EVSE_ESP_FIRMWARE": {"hw_ver_major": 2}}
    assert parse_device_status(HW_TYPE_WBOX, raw)["sw_version"] is None


def test_parse_leak_event_takes_name_and_time_of_the_first_entry():
    record = {"timestamp": 1000, "data": {"0": {"code": 1, "name": "leakage_detected", "value": 5}}}
    assert parse_leak_event(record) == {"name": "leakage_detected", "timestamp": 1000}


def test_parse_leak_event_rejects_broken_records():
    assert parse_leak_event(None) is None
    assert parse_leak_event({}) is None
    assert parse_leak_event({"timestamp": 1, "data": {}}) is None
    assert parse_leak_event({"data": {"0": {"name": "reset"}}}) is None  # нет времени


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


def test_wbox_charging_parses_core_status():
    raw = {
        "common": _online_common(),
        "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(4),  # Charging
        "CHARGER_CMD_GET_TEMPERATURE": {"temperature_avg": 32.5},
    }
    status = parse_device_status(HW_TYPE_WBOX, raw)
    assert status["state"] == 4
    assert status["on"] is True
    assert status["power"] == 3.5
    assert status["max_power"] == 7.0
    assert status["max_power_limit"] == 11.0
    assert status["meter"] == 2.1
    assert status["temperature"] == 32.5


def _wbox_full_raw(**short_overrides) -> dict:
    """Ответ реальной станции (значения сняты read-only 12.11.2026, секреты не включены)."""
    return {
        "common": {**_online_common(), "rssi": -77, "snr": 64},
        "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(
            4, duration=3570, inst_power=0, count_power=6.7, max_power=7.6, max_power_limit=3.4,
            **short_overrides,
        ),
        "CHARGER_CMD_GET_TEMPERATURE": {
            "temperature_avg": 36, "temperature_input": 56, "temperature_output": 42,
        },
        "CHARGER_CMD_CHARGE_SETTINGS": {
            "balance_enabled": 1, "case_temp_limit": 85, "charge_power_limit_enabled": 0,
            "charge_time_limit_enabled": 0, "hour_start_day_tariff": 4,
            "hour_start_night_tariff": 17, "max_balancing_power": 7680,
            "max_charge_power": 10000, "max_charge_time": 60, "use_two_tariff_enabled": 0,
        },
        "CHARGER_CMD_EVSE_SCHEDULER": {
            "start": False, "start_time": 1080, "stop": False, "stop_time": 480,
        },
        "CHARGER_CMD_NFC_TAG_RW": {"total": 1, "tags": ["secret"]},
        "CHARGER_CMD_CHARGE_CONFIRMATION": {"charge_started": True, "nfc_accept": True},
        "CHARGER_DEVICE_EVSE_CARD_NOTIFICATION": {"tag": "880588CB000000"},
        "CHARGER_DEVICE_EVSE_COM_CHANNEL": {
            "link": 0, "lte_rssi": 0, "wa1470_rssi": -75, "wifi_rssi": 0,
        },
        "CHARGER_DEVICE_EVSE_ESP_FIRMWARE": {"hw_ver_id": 1, "hw_ver_major": 2, "hw_ver_minor": 2},
        "CHARGER_DEVICE_EVSE_NVT_FIRMWARE": {"hw_ver_id": 2, "hw_ver_major": 2, "hw_ver_minor": 3},
        "heartbeat": {"temp": 49, "voltage": 3.18},
        "version": {"sw_1": "4.0.1.0", "sw_2": "4.0.1.1", "hw_1": "4.0.0.0", "hw_2": "4.0.0.0"},
        "pair_device": 11526209,
    }


def test_wbox_parses_fields_the_mobile_app_uses():
    status = parse_device_status(HW_TYPE_WBOX, _wbox_full_raw())
    assert status["duration"] == 3570
    assert status["cable_connected"] is True
    assert status["problem"] is False
    assert status["balance_enabled"] is True
    assert status["max_balancing_power"] == 7.68  # Вт -> кВт, граница слайдера мощности
    assert status["use_two_tariff_enabled"] is False
    assert (status["hour_start_day_tariff"], status["hour_start_night_tariff"]) == (4, 17)
    assert status["schedule_start_enabled"] is False
    assert status["schedule_start_time"] == "18:00"
    assert status["schedule_stop_time"] == "08:00"
    assert status["nfc_cards"] == 1
    assert status["nfc_confirmation"] is True
    assert (status["temp_input"], status["temp_output"]) == (56, 42)
    assert status["battery_voltage"] == 3.18
    assert status["pair_device"] == "11526209"
    assert status["sw_version"] == "1.2.2, 2.2.3"


def test_wbox_has_no_fields_the_mobile_app_does_not_show():
    """Пределы мощности, времени и температуры корпуса, сигналы и версии плат в приложении
    не показываются — в статус не попадают."""
    status = parse_device_status(HW_TYPE_WBOX, _wbox_full_raw())
    for key in ("charge_power_limit_enabled", "max_charge_power", "charge_time_limit_enabled",
                "max_charge_time", "case_temp_limit", "last_card", "rssi", "snr", "board_temp",
                "wa1470_rssi", "lte_rssi", "wifi_rssi", "sw_version_1", "hw_version_1",
                "esp_fw", "nvt_fw"):
        assert key not in status, key


def test_wbox_settings_unknown_without_settings_object():
    raw = {"common": _online_common(), "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(4)}
    status = parse_device_status(HW_TYPE_WBOX, raw)
    assert status["balance_enabled"] is None
    assert status["max_balancing_power"] is None
    assert status["use_two_tariff_enabled"] is None


def test_wbox_cable_and_problem_flags_by_state():
    def flags(state):
        raw = {"common": _online_common(), "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(state)}
        status = parse_device_status(HW_TYPE_WBOX, raw)
        return status["cable_connected"], status["problem"]

    assert flags(1) == (False, False)  # свободна
    assert flags(3) == (True, False)  # подключено, ждёт старта
    assert flags(4) == (True, False)  # заряжается
    assert flags(6) == (False, True)  # ошибка УЗО


def test_wbox_missing_settings_give_none_not_zero():
    raw = {"common": _online_common(), "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD": _wbox_short_cmd(3)}
    status = parse_device_status(HW_TYPE_WBOX, raw)
    for key in ("schedule_start_time", "nfc_cards", "sw_version", "duration", "pair_device"):
        assert status[key] is None


def test_parse_charge_session_from_real_record():
    record = {
        "timestamp": 1794231860,
        "data": {"data": {"0100000809FF": 2302, "0100010801FF": 2.121, "0100010802FF": 0.5,
                          "0100010600FF": 6.832}},
    }
    session = parse_charge_session(record)
    assert session == {
        "last_session_end": 1794231860,
        "last_session_duration": 2302,
        "last_session_energy": 2.621,
    }


def test_parse_charge_session_rejects_incomplete_records():
    assert parse_charge_session(None) is None
    assert parse_charge_session({}) is None
    assert parse_charge_session({"timestamp": 1, "data": {"data": {}}}) is None


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
