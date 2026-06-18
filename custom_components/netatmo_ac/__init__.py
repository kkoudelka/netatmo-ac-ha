"""Netatmo Smart AC Controller integration for Home Assistant."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_entry_oauth2_flow

from .api import NetatmoApiClient, NetatmoAuthError, NetatmoApiError
from .const import CONF_HOME_IDS, CONF_MODULE_IDS, DOMAIN
from .coordinator import NacCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one Netatmo account (config entry).

    One coordinator per home, one client shared across coordinators
    (CONTEXT: Isolation Rule, Config Entry Boundary).
    """
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, entry
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    client = NetatmoApiClient(session)

    # Discover all NAC modules up-front so climate platform can create entities
    home_ids: list[str] = entry.data.get(CONF_HOME_IDS, [])
    allowed_module_ids: set[str] = set(entry.data.get(CONF_MODULE_IDS, []))

    coordinators: dict[str, NacCoordinator] = {}
    modules: dict[str, Any] = {}  # module_id → NacModule

    for home_id in home_ids:
        try:
            nac_modules = await client.async_get_nac_modules(home_id)
        except NetatmoAuthError as err:
            raise ConfigEntryAuthFailed from err
        except NetatmoApiError as err:
            raise ConfigEntryNotReady(f"Could not reach Netatmo for home {home_id}: {err}") from err

        home_modules = [m for m in nac_modules if m.module_id in allowed_module_ids]
        for mod in home_modules:
            modules[mod.module_id] = mod

        coordinator = NacCoordinator(hass, client, home_id, entry, home_modules)
        try:
            await coordinator.async_config_entry_first_refresh()
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            raise ConfigEntryNotReady(f"Initial poll failed for home {home_id}") from err

        coordinators[home_id] = coordinator

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinators": coordinators,
        "modules": modules,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
