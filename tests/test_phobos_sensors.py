"""Набор сенсоров электросчётчика зависит от типа прибора, а не от того, онлайн ли он."""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.waviot_home.const import DOMAIN
from custom_components.waviot_home.device import (
    WaviotDeviceInfo,
    has_multiple_tariffs,
    is_three_phase,
)
from custom_components.waviot_home.sensor import (
    PHOBOS_SENSORS,
    WaviotSpecSensor,
    phobos_specs_for,
)


def _device(hw_type: str) -> WaviotDeviceInfo:
    device = WaviotDeviceInfo.from_api({"modem_id": 1, "hw_type": hw_type})
    assert device is not None
    return device


def _keys(hw_type: str) -> set[str]:
    return {spec.key for spec in phobos_specs_for(_device(hw_type))}


def test_spec_keys_are_unique():
    keys = [spec.key for spec in PHOBOS_SENSORS]
    assert len(keys) == len(set(keys))


def test_existing_unique_ids_are_preserved():
    """unique_id = {modem}_{key}: ключи прежних сенсоров менять нельзя."""
    keys = {spec.key for spec in PHOBOS_SENSORS}
    assert {"meter", "voltage", "power", "amperage"} <= keys


def test_three_phase_meter_gets_full_set_like_reference_meter():
    keys = _keys("phobos3L_wa_nvt_stm")
    assert keys == {spec.key for spec in PHOBOS_SENSORS}
    assert {"voltage_b", "voltage_c", "amperage_b", "power_c", "energy_t4"} <= keys


def test_single_phase_single_tariff_meter_set():
    keys = _keys("phobosL_wa_nvt_stm")
    assert {"meter", "energy_t1", "voltage", "amperage", "power"} <= keys
    assert not keys & {"energy_t2", "energy_t3", "energy_t4"}
    assert not keys & {"voltage_b", "voltage_c", "amperage_b", "amperage_c", "power_b", "power_c"}


def test_single_phase_multi_tariff_meter_keeps_four_tariffs_but_no_phases():
    keys = _keys("phobosL_wa_stm")
    assert {f"energy_t{i}" for i in range(1, 5)} <= keys
    assert "voltage_b" not in keys


def test_tariff_rule_matches_mobile_app():
    assert has_multiple_tariffs("phobos3_wa_stm")
    assert has_multiple_tariffs("phobos3L_dualcore_stm")
    assert has_multiple_tariffs("phobosL_wa_stm")
    assert not has_multiple_tariffs("phobosL_wa_nvt_stm")


# --- имена и видимость фаз ---------------------------------------------------------------

def _sensor(hw_type: str, key: str) -> WaviotSpecSensor:
    spec = next(s for s in PHOBOS_SENSORS if s.key == key)
    return WaviotSpecSensor(MagicMock(), _device(hw_type), spec)


def test_phases_stay_on_main_device_no_child_devices():
    for key in ("voltage", "voltage_b", "voltage_c", "power_c"):
        info = _sensor("phobos3_wa_stm", key).device_info
        assert info["identifiers"] == {(DOMAIN, "1")}
        assert "via_device" not in info


def test_three_phase_names_include_phase_letter():
    assert _sensor("phobos3_wa_stm", "voltage").name == "Напряжение, фаза A"
    assert _sensor("phobos3_wa_stm", "amperage_b").name == "Ток, фаза B"
    assert _sensor("phobos3_wa_stm", "power_c").name == "Мощность, фаза C"


def test_single_phase_meter_names_have_no_phase_letter():
    assert _sensor("phobosL_wa_nvt_stm", "voltage").name == "Напряжение"


def test_three_phase_b_and_c_disabled_by_default_a_enabled():
    assert _sensor("phobos3_wa_stm", "voltage").entity_registry_enabled_default is True
    assert _sensor("phobos3_wa_stm", "voltage_b").entity_registry_enabled_default is False
    assert _sensor("phobos3_wa_stm", "voltage_c").entity_registry_enabled_default is False
    assert _sensor("phobos3_wa_stm", "power_b").entity_registry_enabled_default is False


def test_totals_and_energy_enabled_by_default():
    for key in ("meter", "energy_t1", "battery_voltage"):
        assert _sensor("phobos3_wa_stm", key).entity_registry_enabled_default is True


def test_power_sum_disabled_by_default():
    assert _sensor("phobos3_wa_stm", "power_sum").entity_registry_enabled_default is False


def test_tariffs_t2_t3_t4_disabled_by_default():
    for key in ("energy_t2", "energy_t3", "energy_t4"):
        assert _sensor("phobos3_wa_stm", key).entity_registry_enabled_default is False


def test_single_phase_meter_phase_a_enabled():
    assert _sensor("phobosL_wa_nvt_stm", "voltage").entity_registry_enabled_default is True


def test_three_phase_rule():
    assert is_three_phase("phobos3L_dualcore_stm")
    assert not is_three_phase("phobosL_wa_nvt_stm")
