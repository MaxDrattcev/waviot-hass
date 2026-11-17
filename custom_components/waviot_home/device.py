"""Представление устройства Waviot внутри интеграции."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .const import (
    HW_TYPE_AQUA,
    HW_TYPE_BETAR,
    HW_TYPE_GEFEST,
    HW_TYPE_LADOGA,
    HW_TYPE_PHOBOS,
    HW_TYPE_WALLHUB,
    HW_TYPE_WBOX,
    detect_hw_type,
)

# Названия типов — те же, что показывает мобильное приложение WAVIoT HOME,
# чтобы пользователь видел одинаковые имена в приложении и в Home Assistant.
_KIND_TITLES = {
    HW_TYPE_WBOX: "Зарядка",
    HW_TYPE_LADOGA: "Датчик протечки LA-25",
    HW_TYPE_AQUA: "Aqua-2",
    HW_TYPE_BETAR: "Бетар",
    HW_TYPE_GEFEST: "Гефест",
    HW_TYPE_WALLHUB: "СмартХаб SH-211",
}

# Фобос в приложении называется по модификации (подстрока hw_type, регистр не важен).
# Порядок важен: более специфичные подстроки раньше.
_PHOBOS_TITLES = (
    ("phobos3ti_wa_nvt_stm", "TEX-3"),
    ("phobos3l_wa_nvt_stm", "TEX-3"),
    ("phobos3", "ФОБОС-3"),  # остальные трёхфазные, в т.ч. phobos3L_dualcore_stm
    ("phobosl_wa_nvt_stm", "TEX-1"),
    ("phobosl_wa_stm", "TEX-1"),
)
_PHOBOS_FALLBACK_TITLE = "Фобос"


def device_type_title(kind: str, hw_type: str) -> str:
    """Название типа устройства как в мобильном приложении."""
    if kind == HW_TYPE_PHOBOS:
        lowered = hw_type.lower()
        for fragment, title in _PHOBOS_TITLES:
            if fragment in lowered:
                return title
        return _PHOBOS_FALLBACK_TITLE
    return _KIND_TITLES.get(kind, kind)


def has_multiple_tariffs(hw_type: str) -> bool:
    """Многотарифный ли счётчик — правило мобильного приложения.

    Приложение показывает T1–T4 для `phobos3*` и `phobosL_wa_stm`, у остальных только T1.
    """
    lowered = hw_type.lower()
    return "phobos3" in lowered or "phobosl_wa_stm" in lowered


def is_three_phase(hw_type: str) -> bool:
    """Трёхфазный ли счётчик. Явного признака в API нет — определяем по названию модели."""
    return "phobos3" in hw_type.lower()


@dataclass(frozen=True)
class WaviotDeviceInfo:
    modem_id: str
    hw_type: str
    kind: str

    @property
    def title(self) -> str:
        """Имя устройства в HA: название типа и номер, например «Зарядка 11968025»."""
        return f"{device_type_title(self.kind, self.hw_type)} {self.modem_id}"

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "WaviotDeviceInfo | None":
        hw_type = raw.get("hw_type", "")
        kind = detect_hw_type(hw_type)
        if kind is None:
            return None
        return cls(modem_id=str(raw["modem_id"]), hw_type=hw_type, kind=kind)
