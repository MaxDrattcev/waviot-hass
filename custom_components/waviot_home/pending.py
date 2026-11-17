"""Ожидаемое значение настройки после записи на устройство.

Облако обновляет кэш настроек не сразу после выполнения команды (приложение тоже
подставляет записанное значение само). Пока облако отдаёт прежнее значение, показываем
то, что отправили; как только облако значение изменило — верим облаку.
"""
from __future__ import annotations

import time
from typing import Any

PENDING_TTL_SECONDS = 1800


class PendingValue:
    def __init__(self) -> None:
        self._active = False
        self._value: Any = None
        self._baseline: Any = None
        self._until = 0.0

    def set(self, value: Any, baseline: Any) -> None:
        """value — отправленное значение, baseline — что облако отдавало до записи."""
        self._active = True
        self._value = value
        self._baseline = baseline
        self._until = time.monotonic() + PENDING_TTL_SECONDS

    def clear(self) -> None:
        self._active = False

    def resolve(self, cloud: Any) -> Any:
        """Значение для показа при текущем значении облака."""
        if not self._active:
            return cloud
        if time.monotonic() > self._until or cloud != self._baseline:
            self.clear()
            return cloud
        return self._value
