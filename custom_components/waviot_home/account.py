"""Управление токенами одного Waviot-аккаунта: ротация и восстановление мёртвой сессии."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from .api import WaviotApiError, WaviotAuthError, WaviotClient, WaviotTokens
from .const import CONF_JWT, CONF_LOGIN, CONF_REFRESH_TOKEN

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


class WaviotAccount:
    """Держит актуальные токены аккаунта и умеет их обновлять.

    В config entry хранятся логин и пароль, поэтому при окончательно мёртвой
    refresh-сессии сначала пробуем тихий полный релогин сохранённым паролем и
    только если он тоже не подходит — просим пользователя переавторизоваться
    (ConfigEntryAuthFailed).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: WaviotClient) -> None:
        self._hass = hass
        self._entry = entry
        self._client = client
        self._tokens = WaviotTokens(
            jwt=entry.data[CONF_JWT], refresh=entry.data[CONF_REFRESH_TOKEN]
        )

    @property
    def jwt(self) -> str:
        return self._tokens.jwt

    async def async_refresh(self) -> str:
        """Обновить токены; при мёртвой сессии — попытаться полный релогин паролем."""
        try:
            new_tokens = await self._client.refresh_tokens(self._tokens)
        except WaviotApiError as err:
            if not err.is_permanent_session_error:
                raise
            _LOGGER.warning(
                "Waviot refresh token для %s мёртв (%s), пробую полный релогин",
                self._entry.data[CONF_LOGIN],
                err,
            )
            try:
                new_tokens = await self._client.authorize(
                    self._entry.data[CONF_LOGIN], self._entry.data[CONF_PASSWORD]
                )
            except (WaviotApiError, WaviotAuthError) as reauth_err:
                raise ConfigEntryAuthFailed(
                    "Сессия Waviot истекла, сохранённый пароль тоже не подошёл"
                ) from reauth_err

        self._tokens = new_tokens
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_JWT: new_tokens.jwt,
                CONF_REFRESH_TOKEN: new_tokens.refresh,
            },
        )
        return new_tokens.jwt

    async def async_call(self, func: Callable[[str], Awaitable[T]]) -> T:
        """Вызвать func(jwt); при 401 обновить токен и повторить один раз."""
        try:
            return await func(self.jwt)
        except WaviotApiError as err:
            if err.status != 401:
                raise
        jwt = await self.async_refresh()
        return await func(jwt)
