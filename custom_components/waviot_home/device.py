"""Представление устройства Waviot внутри интеграции."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .const import detect_hw_type


@dataclass(frozen=True)
class WaviotDeviceInfo:
    modem_id: str
    hw_type: str
    kind: str

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "WaviotDeviceInfo | None":
        hw_type = raw.get("hw_type", "")
        kind = detect_hw_type(hw_type)
        if kind is None:
            return None
        return cls(modem_id=str(raw["modem_id"]), hw_type=hw_type, kind=kind)
