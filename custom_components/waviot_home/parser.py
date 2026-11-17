"""Разбор статуса устройства Waviot по типу."""
from __future__ import annotations

import time
from typing import Any

from .const import (
    CHARGER_ONLINE_SECONDS_DEFAULT,
    CHARGER_ONLINE_SECONDS_RSSI_50,
    EVSE_ERROR_STATES,
    EVSE_STATE_CHARGING,
    EVSE_STATE_WAITING_START,
    HW_TYPE_AQUA,
    HW_TYPE_BETAR,
    HW_TYPE_GEFEST,
    HW_TYPE_LADOGA,
    HW_TYPE_PHOBOS,
    HW_TYPE_WALLHUB,
    HW_TYPE_WBOX,
)

# Устройства на батарейках отчитываются редко, поэтому порог офлайна большой — 2 суток.
DEFAULT_ONLINE_SECONDS = 86400 * 2


def _get_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _get_optional_float(value: Any) -> float | None:
    """Число или None: отсутствие данных не должно превращаться в ложный 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _get_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


# Энергия A+ по тарифам: OBIS 1.0.1.8.{1..4}.255 (как в мобильном приложении).
_PHOBOS_TARIFF_KEYS = {f"energy_t{i}": f"01000108{i:02d}FF" for i in range(1, 5)}

# Поле мгновенного профиля (report/history, profile 1.0.94.7.0.255) -> ключ в статусе.
# Единицы как в ответе облака: напряжение В, ток А, P/Q/S — Вт/вар/ВА, частота Гц.
_PHOBOS_INSTANT_FIELDS = {
    "voltage": "32.7.0",
    "voltage_b": "52.7.0",
    "voltage_c": "72.7.0",
    "amperage": "31.7.0",
    "amperage_b": "51.7.0",
    "amperage_c": "71.7.0",
    "power": "21.7.0",
    "power_b": "41.7.0",
    "power_c": "61.7.0",
    "power_sum": "1.7.0",
    "reactive_a": "23.7.0",
    "reactive_b": "43.7.0",
    "reactive_c": "63.7.0",
    "reactive_sum": "3.7.0",
    "apparent_a": "29.7.0",
    "apparent_b": "49.7.0",
    "apparent_c": "69.7.0",
    "apparent_sum": "9.7.0",
    "frequency": "14.7.0",
    "neutral_current": "91.7.0",
}


def apply_instant(status: dict[str, Any], record: dict[str, Any]) -> None:
    """Дополнить статус счётчика значениями мгновенного профиля.

    Пришедшее в мгновенном профиле имеет приоритет над ключами настроек.
    """
    for key, obis in _PHOBOS_INSTANT_FIELDS.items():
        value = _get_optional_float(record.get(obis))
        if value is not None:
            status[key] = value


def _get_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _to_kilo(value: Any) -> float | None:
    """Вт -> кВт; None, если значения нет."""
    number = _get_optional_float(value)
    return None if number is None else number / 1000


def _minutes_to_hhmm(value: Any) -> str | None:
    """Минуты от полуночи -> «ЧЧ:ММ» (расписание зарядки; формат выведен по значениям 1080 и 480)."""
    minutes = _get_optional_float(value)
    if minutes is None or not 0 <= minutes < 24 * 60:
        return None
    total = int(minutes)
    return f"{total // 60:02d}:{total % 60:02d}"


def _firmware_version(firmware: dict[str, Any]) -> str | None:
    """Версия прошивки контроллера в формате приложения: «id.major.minor» (например 1.2.6)."""
    parts = [
        _get_optional_float(firmware.get(key))
        for key in ("hw_ver_id", "hw_ver_major", "hw_ver_minor")
    ]
    if any(part is None for part in parts):
        return None
    return ".".join(str(int(part)) for part in parts)


def _controller_versions(raw: dict[str, Any], *keys: str) -> str | None:
    """Версии контроллеров через запятую, как в карточке «Информация» приложения."""
    versions = [_firmware_version(_as_dict(raw.get(key))) for key in keys]
    present = [version for version in versions if version]
    return ", ".join(present) if present else None


def parse_leak_event(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Последнее событие датчика протечки из записи water7.event: имя и время."""
    if not isinstance(record, dict):
        return None
    entries = _as_dict(record.get("data"))
    entry = _as_dict(entries.get("0")) or next(
        (value for value in entries.values() if isinstance(value, dict)), {}
    )
    name = _get_optional_str(entry.get("name"))
    timestamp = _get_optional_float(record.get("timestamp"))
    if name is None or timestamp is None:
        return None
    return {"name": name, "timestamp": timestamp}


def parse_charge_session(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Последняя завершённая сессия зарядки из записи electro5.data_profile_charge.

    Длительность — OBIS 0100000809FF (с), энергия по тарифам — 0100010801FF и
    0100010802FF (кВт·ч), время — timestamp записи (конец сессии).
    """
    if not isinstance(record, dict):
        return None
    data = _as_dict(_as_dict(record.get("data")).get("data"))
    timestamp = _get_optional_float(record.get("timestamp"))
    duration = _get_optional_float(data.get("0100000809FF"))
    energy_t1 = _get_optional_float(data.get("0100010801FF"))
    energy_t2 = _get_optional_float(data.get("0100010802FF"))
    if timestamp is None or duration is None or (energy_t1 is None and energy_t2 is None):
        return None
    return {
        "last_session_end": timestamp,
        "last_session_duration": duration,
        "last_session_energy": (energy_t1 or 0.0) + (energy_t2 or 0.0),
    }


def parse_charge_history(records: Any) -> list[dict[str, float]]:
    """Все завершённые сессии зарядки из ответа electro5.data_profile_charge.

    Возвращает записи по возрастанию времени окончания: end — время окончания (с),
    duration — длительность (с), t1 и t2 — энергия по тарифам (кВт·ч).
    Записи без времени или длительности пропускаются.
    """
    sessions: list[dict[str, float]] = []
    for record in records if isinstance(records, list) else []:
        data = _as_dict(_as_dict(_as_dict(record).get("data")).get("data"))
        end = _get_optional_float(_as_dict(record).get("timestamp"))
        duration = _get_optional_float(data.get("0100000809FF"))
        t1 = _get_optional_float(data.get("0100010801FF"))
        t2 = _get_optional_float(data.get("0100010802FF"))
        if end is None or duration is None or (t1 is None and t2 is None):
            continue
        sessions.append({"end": end, "duration": duration, "t1": t1 or 0.0, "t2": t2 or 0.0})
    return sorted(sessions, key=lambda session: session["end"])


def _get_common(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    common = raw.get("common")
    return common if isinstance(common, dict) else None


def _get_heartbeat_voltage(raw: dict[str, Any]) -> float:
    heartbeat = raw.get("heartbeat")
    if isinstance(heartbeat, dict):
        return _get_float(heartbeat.get("voltage"))
    return 0.0


def _is_offline(common: dict[str, Any] | None) -> bool:
    """Без блока common считаем офлайном; иначе — если данные старше 2 суток."""
    if common is None:
        return True
    return time.time() - _get_float(common.get("time_saved")) > DEFAULT_ONLINE_SECONDS


def _is_charger_offline(common: dict[str, Any] | None) -> bool:
    """Порог для wbox: при rssi==50 — 20 мин, иначе 120 мин (см. const.py)."""
    if common is None:
        return True
    time_saved = _get_float(common.get("time_saved"))
    if "time_saved" not in common:
        return True
    age = time.time() - time_saved
    if age < 0:
        return True
    threshold = (
        CHARGER_ONLINE_SECONDS_RSSI_50
        if _get_float(common.get("rssi")) == 50
        else CHARGER_ONLINE_SECONDS_DEFAULT
    )
    return age >= threshold


def parse_device_status(kind: str, raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Вернуть метрики устройства, либо None если офлайн/данных нет."""
    if raw is None:
        return None
    common = _get_common(raw)

    if kind == HW_TYPE_PHOBOS:
        if _is_offline(common):
            return None
        heartbeat = raw.get("heartbeat")
        heartbeat = heartbeat if isinstance(heartbeat, dict) else {}
        version = raw.get("version")
        version = version if isinstance(version, dict) else {}
        return {
            "on": bool(_get_float(raw.get("0.0.96.3.10.255_2"))),
            # Энергия — накопительная, в Вт·ч (облако отдаёт сырое значение).
            "meter": _get_optional_float(raw.get("0100010800FF")),
            **{
                key: _get_optional_float(raw.get(obis))
                for key, obis in _PHOBOS_TARIFF_KEYS.items()
            },
            # Мгновенные значения; если счётчик их не присылает — None, а не 0.
            "voltage": _get_optional_float(raw.get("0100200700FF")),
            "power": _get_optional_float(raw.get("0100150700FF")),
            "amperage": _get_optional_float(raw.get("01001F0700FF")),
            # Настройки: облако отдаёт их только после чтения прибором (например, из
            # приложения), иначе ключа нет и значение неизвестно.
            "instant_period": _get_optional_float(raw.get("1.0.0.8.9.255_2")),
            "power_limit": _get_optional_float(raw.get("0.0.17.0.0.255_3")),
            "time_turn_off": _get_optional_float(raw.get("0.0.17.0.0.255_6")),
            "time_turn_on": _get_optional_float(raw.get("0.0.17.0.0.255_7")),
            "powersharing_enabled": _get_optional_bool(
                _as_dict(raw.get("powersharing")).get("state")
            ),
            "powersharing_max": _to_kilo(_as_dict(raw.get("powersharing")).get("p_max")),
            "battery_voltage": _get_optional_float(heartbeat.get("voltage")),
            "sw_version": _get_optional_str(version.get("sw")),
        }

    if kind == HW_TYPE_AQUA:
        if _is_offline(common):
            return None
        return {
            "meter": _get_float(raw.get("liter_forw_VAL")),
            "voltage": _get_heartbeat_voltage(raw),
        }

    if kind == HW_TYPE_GEFEST:
        if _is_offline(common):
            return None
        return {
            "meter": _get_float(raw.get("full_1")) / 1000,
            "voltage": _get_heartbeat_voltage(raw),
        }

    if kind == HW_TYPE_BETAR:
        if _is_offline(common):
            return None
        # voltage у betar добавляется отдельно координатором (GetVoltBetar) — тут его нет.
        # Показание газа и напряжение батареи берутся из EAV (как в мобильном приложении);
        # `full_1` в ответе настроек в другом масштабе, поэтому его не используем.
        return {"meter": None, "voltage": None}

    if kind == HW_TYPE_LADOGA:
        if _is_offline(common):
            return None
        return {"voltage": _get_heartbeat_voltage(raw)}

    if kind == HW_TYPE_WALLHUB:
        if _is_offline(common):
            return None
        sensor = _as_dict(raw.get("SSERIAL_CMD_TEMPERATURE"))
        indication = _as_dict(raw.get("SSERIAL_CMD_INDICATION_TIMEOUT"))
        return {
            "temperature": _get_optional_float(sensor.get("temperature")),
            "humidity": _get_optional_float(sensor.get("humidity")),
            # Версия ПО хаба — версия прошивки ESP, как в приложении
            "sw_version": _controller_versions(raw, "CHARGER_DEVICE_EVSE_ESP_FIRMWARE"),
            "indication_timeout": _get_optional_float(indication.get("timeout")),
            "indication_infinite": _get_optional_bool(indication.get("infinite")),
        }

    if kind == HW_TYPE_WBOX:
        if _is_charger_offline(common):
            return None
        short = raw.get("CHARGER_DEVICE_EVSE_SHORT_NEW_CMD")
        if not isinstance(short, dict):
            return None
        state = int(_get_float(short.get("state")))
        temp = _as_dict(raw.get("CHARGER_CMD_GET_TEMPERATURE"))
        settings = _as_dict(raw.get("CHARGER_CMD_CHARGE_SETTINGS"))
        schedule = _as_dict(raw.get("CHARGER_CMD_EVSE_SCHEDULER"))
        heartbeat = _as_dict(raw.get("heartbeat"))
        pair_device = raw.get("pair_device")
        return {
            "state": state,
            "on": state == EVSE_STATE_CHARGING,
            "power": _get_float(short.get("inst_power")),
            "max_power": _get_float(short.get("max_power")),
            "max_power_limit": _get_float(short.get("max_power_limit")),
            "meter": _get_float(short.get("count_power")),
            "temperature": _get_float(temp.get("temperature_avg")),
            "temp_input": _get_optional_float(temp.get("temperature_input")),
            "temp_output": _get_optional_float(temp.get("temperature_output")),
            # Сессия и состояние
            "duration": _get_optional_float(short.get("duration")),
            "cable_connected": state in (EVSE_STATE_WAITING_START, EVSE_STATE_CHARGING),
            "problem": state in EVSE_ERROR_STATES,
            # Настройки, которые есть в приложении. Мощности в Вт -> кВт.
            "balance_enabled": _get_optional_bool(settings.get("balance_enabled")),
            # Максимум балансировки приложение использует как границу слайдера мощности
            "max_balancing_power": _to_kilo(settings.get("max_balancing_power")),
            "use_two_tariff_enabled": _get_optional_bool(settings.get("use_two_tariff_enabled")),
            "hour_start_day_tariff": _get_optional_float(settings.get("hour_start_day_tariff")),
            "hour_start_night_tariff": _get_optional_float(
                settings.get("hour_start_night_tariff")
            ),
            # Расписание
            "schedule_start_enabled": _get_optional_bool(schedule.get("start")),
            "schedule_start_time": _minutes_to_hhmm(schedule.get("start_time")),
            "schedule_stop_enabled": _get_optional_bool(schedule.get("stop")),
            "schedule_stop_time": _minutes_to_hhmm(schedule.get("stop_time")),
            # NFC и привязанный счётчик
            "nfc_cards": _get_optional_float(
                _as_dict(raw.get("CHARGER_CMD_NFC_TAG_RW")).get("total")
            ),
            "nfc_confirmation": _get_optional_bool(
                _as_dict(raw.get("CHARGER_CMD_CHARGE_CONFIRMATION")).get("nfc_accept")
            ),
            "pair_device": None if pair_device is None else str(pair_device),
            # Информация: версия ПО (контроллеры ESP и NVT), последние данные, батарея
            "sw_version": _controller_versions(
                raw, "CHARGER_DEVICE_EVSE_ESP_FIRMWARE", "CHARGER_DEVICE_EVSE_NVT_FIRMWARE"
            ),
            "battery_voltage": _get_optional_float(heartbeat.get("voltage")),
        }

    return None
