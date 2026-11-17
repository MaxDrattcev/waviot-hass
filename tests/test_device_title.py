"""Имена устройств должны совпадать с мобильным приложением: название типа + номер."""
import pytest

from custom_components.waviot_home.device import WaviotDeviceInfo


@pytest.mark.parametrize(
    ("raw_hw_type", "expected"),
    [
        ("wbox_nvt_wa_stm", "Зарядка 100"),
        ("ladoga_stm", "Датчик протечки LA-25 100"),
        ("aqua2_stm", "Aqua-2 100"),
        ("betar_stm", "Бетар 100"),
        ("gefest_stm", "Гефест 100"),
        ("wallhub_stm", "СмартХаб SH-211 100"),
        ("phobosL_wa_nvt_stm", "TEX-1 100"),
        ("phobosL_wa_stm", "TEX-1 100"),
        ("phobos3_wa_stm", "ФОБОС-3 100"),
        ("phobos3ti_wa_nvt_stm", "TEX-3 100"),
        ("phobos3L_wa_nvt_stm", "TEX-3 100"),
        ("phobos3L_dualcore_stm", "ФОБОС-3 100"),
        ("phobos3ti_stm", "ФОБОС-3 100"),
        ("phobos_unknown_stm", "Фобос 100"),
    ],
)
def test_title_matches_mobile_app(raw_hw_type, expected):
    device = WaviotDeviceInfo.from_api({"modem_id": 100, "hw_type": raw_hw_type})
    assert device is not None
    assert device.title == expected
