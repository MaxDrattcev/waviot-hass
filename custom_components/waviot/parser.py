"""Разбор статуса устройства Waviot по типу."""
from __future__ import annotations

import time
from typing import Any

from .const import (
    CHARGER_ONLINE_SECONDS_DEFAULT,
    CHARGER_ONLINE_SECONDS_RSSI_50,
    EVSE_STATE_CHARGING,
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
        return {
            "on": bool(_get_float(raw.get("0.0.96.3.10.255_2"))),
            "meter": _get_float(raw.get("0100010800FF")),
            "voltage": _get_float(raw.get("0100200700FF")),
            "power": _get_float(raw.get("0100150700FF")),
            "amperage": _get_float(raw.get("01001F0700FF")),
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
        return {"meter": _get_float(raw.get("full_1")) / 1000}

    if kind == HW_TYPE_LADOGA:
        if _is_offline(common):
            return None
        return {"voltage": _get_heartbeat_voltage(raw)}

    if kind == HW_TYPE_WALLHUB:
        if _is_offline(common):
            return None
        sensor = raw.get("SSERIAL_CMD_TEMPERATURE")
        if isinstance(sensor, dict):
            return {
                "temperature": _get_float(sensor.get("temperature")),
                "humidity": _get_float(sensor.get("humidity")),
            }
        return {"temperature": 0.0, "humidity": 0.0}

    if kind == HW_TYPE_WBOX:
        if _is_charger_offline(common):
            return None
        short = raw.get("CHARGER_DEVICE_EVSE_SHORT_NEW_CMD")
        if not isinstance(short, dict):
            return None
        state = int(_get_float(short.get("state")))
        temp = raw.get("CHARGER_CMD_GET_TEMPERATURE")
        temperature = _get_float(temp.get("temperature_avg")) if isinstance(temp, dict) else 0.0
        return {
            "state": state,
            "on": state == EVSE_STATE_CHARGING,
            "power": _get_float(short.get("inst_power")),
            "max_power": _get_float(short.get("max_power")),
            "max_power_limit": _get_float(short.get("max_power_limit")),
            "meter": _get_float(short.get("count_power")),
            "temperature": temperature,
        }

    return None
