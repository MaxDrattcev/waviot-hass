"""Config flow интеграции Waviot."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WaviotApiError, WaviotAuthError, WaviotClient
from .const import CONF_JWT, CONF_LOGIN, CONF_REFRESH_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_LOGIN): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class WaviotConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow для добавления аккаунта Waviot."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_LOGIN])
            self._abort_if_unique_id_configured()

            client = WaviotClient(async_get_clientsession(self.hass))
            try:
                tokens = await client.authorize(
                    user_input[CONF_LOGIN], user_input[CONF_PASSWORD]
                )
            except WaviotAuthError:
                errors["base"] = "invalid_auth"
            except WaviotApiError as err:
                _LOGGER.warning("Ошибка авторизации Waviot: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_LOGIN],
                    data={
                        CONF_LOGIN: user_input[CONF_LOGIN],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_JWT: tokens.jwt,
                        CONF_REFRESH_TOKEN: tokens.refresh,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            client = WaviotClient(async_get_clientsession(self.hass))
            try:
                tokens = await client.authorize(
                    reauth_entry.data[CONF_LOGIN], user_input[CONF_PASSWORD]
                )
            except WaviotAuthError:
                errors["base"] = "invalid_auth"
            except WaviotApiError as err:
                _LOGGER.warning("Ошибка повторной авторизации Waviot: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={
                        **reauth_entry.data,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_JWT: tokens.jwt,
                        CONF_REFRESH_TOKEN: tokens.refresh,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"login": reauth_entry.data[CONF_LOGIN]},
        )
