"""Тесты чистой логики координаторов — без реального HA/API."""
from __future__ import annotations

from datetime import timedelta

from custom_components.waviot_home.const import (
    CHARGER_CHARGING_UPDATE_INTERVAL,
    CHARGER_IDLE_UPDATE_INTERVAL,
    EVSE_STATE_CHARGING,
    EVSE_STATE_WAITING_START,
)
from custom_components.waviot_home.coordinator import charging_update_interval


def test_idle_interval_when_nothing_charging():
    statuses = {
        "1": {"state": EVSE_STATE_WAITING_START},
        "2": {"state": EVSE_STATE_WAITING_START},
    }
    assert charging_update_interval(statuses) == timedelta(seconds=CHARGER_IDLE_UPDATE_INTERVAL)


def test_fast_interval_when_at_least_one_charging():
    statuses = {
        "1": {"state": EVSE_STATE_WAITING_START},
        "2": {"state": EVSE_STATE_CHARGING},
    }
    assert charging_update_interval(statuses) == timedelta(seconds=CHARGER_CHARGING_UPDATE_INTERVAL)


def test_idle_interval_when_no_devices_at_all():
    assert charging_update_interval({}) == timedelta(seconds=CHARGER_IDLE_UPDATE_INTERVAL)
