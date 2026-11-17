"""Длительность индикации СмартХаба — те же четыре варианта, что в мобильном приложении."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WaviotConfigEntry
from .account import WaviotAccount
from .api import WaviotApiError, WaviotClient
from .const import DOMAIN, HW_TYPE_WALLHUB
from .coordinator import WaviotMainCoordinator
from .device import WaviotDeviceInfo
from .pending import PendingValue

# Вариант -> данные команды SSERIAL_CMD_INDICATION_TIMEOUT. В приложении «Всегда» это
# значение 65535, которое уходит на провод как {"infinite": true}.
INDICATION_OPTIONS: dict[str, dict] = {
    "Никогда": {"timeout": 0},
    "30 с": {"timeout": 30},
    "5 минут": {"timeout": 300},
    "Всегда": {"infinite": True},
}


def indication_option(timeout: float | None, infinite: bool | None) -> str | None:
    """Вариант по текущим настройкам хаба; нестандартное значение — None (неизвестно)."""
    if infinite:
        return "Всегда"
    if timeout is None:
        return None
    for option, data in INDICATION_OPTIONS.items():
        if data.get("timeout") == int(timeout):
            return option
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WaviotConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    async_add_entities(
        WaviotIndicationSelect(runtime.main_coordinator, runtime.client, runtime.account, device)
        for device in runtime.devices
        if device.kind == HW_TYPE_WALLHUB
    )


class WaviotIndicationSelect(CoordinatorEntity[WaviotMainCoordinator], SelectEntity):
    """Как долго светится индикация хаба после события."""

    _attr_has_entity_name = True
    _attr_name = "Индикация: длительность"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:led-on"

    def __init__(
        self,
        coordinator: WaviotMainCoordinator,
        client: WaviotClient,
        account: WaviotAccount,
        device: WaviotDeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._account = account
        self._device = device
        self._pending = PendingValue()
        self._attr_unique_id = f"{device.modem_id}_indication"
        self._attr_options = list(INDICATION_OPTIONS)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.modem_id)},
            name=device.title,
            manufacturer="Waviot",
            model=device.kind,
        )

    @property
    def _status(self) -> dict | None:
        return self.coordinator.data.get(self._device.modem_id)

    @property
    def available(self) -> bool:
        return super().available and self._status is not None

    @property
    def _cloud_option(self) -> str | None:
        status = self._status or {}
        return indication_option(status.get("indication_timeout"), status.get("indication_infinite"))

    @property
    def current_option(self) -> str | None:
        return self._pending.resolve(self._cloud_option)

    async def async_select_option(self, option: str) -> None:
        data = INDICATION_OPTIONS[option]
        baseline = self._cloud_option
        try:
            await self._account.async_call(
                lambda jwt: self._client.wallhub_write_indication(
                    self._device.modem_id, jwt, data
                )
            )
        except WaviotApiError as err:
            raise HomeAssistantError(f"Не удалось изменить длительность индикации: {err}") from err
        self._pending.set(option, baseline)
        self.async_write_ha_state()
