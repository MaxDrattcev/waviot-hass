"""Тесты async_setup_entry при сбоях облака на старте — реальный Waviot API не вызывается."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from custom_components.waviot_home import async_setup_entry
from custom_components.waviot_home.api import WaviotApiError, WaviotAuthError

TRANSIENT_ERRORS = [
    WaviotApiError(503, "service unavailable"),
    WaviotAuthError("empty tokens in waviot response"),
    aiohttp.ClientConnectionError("connection refused"),
    TimeoutError(),
]


@pytest.fixture
def account() -> MagicMock:
    acc = MagicMock()
    acc.async_refresh = AsyncMock(return_value="fake-jwt")
    return acc


@pytest.fixture
def client() -> MagicMock:
    cl = MagicMock()
    cl.get_devices = AsyncMock(return_value=[])
    return cl


@pytest.fixture(autouse=True)
def patched_dependencies(account: MagicMock, client: MagicMock):
    with (
        patch("custom_components.waviot_home.WaviotAccount", return_value=account),
        patch("custom_components.waviot_home.WaviotClient", return_value=client),
        patch("custom_components.waviot_home.async_get_clientsession"),
    ):
        yield


@pytest.mark.parametrize("error", TRANSIENT_ERRORS, ids=lambda e: type(e).__name__)
async def test_refresh_failure_raises_config_entry_not_ready(account, error):
    account.async_refresh.side_effect = error

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(MagicMock(), MagicMock())


@pytest.mark.parametrize("error", TRANSIENT_ERRORS, ids=lambda e: type(e).__name__)
async def test_get_devices_failure_raises_config_entry_not_ready(client, error):
    client.get_devices.side_effect = error

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(MagicMock(), MagicMock())


async def test_auth_failure_is_not_swallowed_into_not_ready(account):
    account.async_refresh.side_effect = ConfigEntryAuthFailed("session expired")

    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(MagicMock(), MagicMock())
