"""Diagnostics platform for Netatmo Smart AC Controller.

Exposes operational telemetry per account (config entry) for troubleshooting.
Sensitive data (tokens, module/home IDs) are redacted by default
(CONTEXT: Diagnostics Contents Rule, Privacy Redaction Rule).

Note: HA's diagnostics platform contract requires async_get_config_entry_diagnostics
and async_get_device_diagnostics to be coroutines even when no I/O is performed.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOME_IDS, CONF_MODULE_IDS, DOMAIN
from .coordinator import NacCoordinator

_REDACTED = "**redacted**"
_TOKEN_KEYS = {"token", "access_token", "refresh_token", "auth_implementation"}


def _entry_diagnostics(entry: ConfigEntry, coordinators: dict[str, NacCoordinator]) -> dict[str, Any]:
    coordinator_diags: dict[str, Any] = {}
    for idx, (home_id, coord) in enumerate(coordinators.items()):
        diag = dict(coord.diagnostics)
        diag["home_id"] = _REDACTED
        coordinator_diags[f"home_{idx}"] = diag

    return {
        "entry_id": entry.entry_id,
        "entry_data_redacted": async_redact_data(
            dict(entry.data), _TOKEN_KEYS | {CONF_HOME_IDS, CONF_MODULE_IDS}
        ),
        "homes_configured": len(entry.data.get(CONF_HOME_IDS, [])),
        "modules_configured": len(entry.data.get(CONF_MODULE_IDS, [])),
        "coordinators": coordinator_diags,
    }


def _device_diagnostics(
    entry: ConfigEntry,
    coordinators: dict[str, NacCoordinator],
    modules: dict[str, Any],
    device: Any,
) -> dict[str, Any]:
    device_identifiers: set[tuple] = device.identifiers or set()
    module = next(
        (mod for mod in modules.values() if (DOMAIN, mod.module_id) in device_identifiers),
        None,
    )
    if module is None:
        return {"error": "Device not found in integration data"}

    coord = coordinators.get(module.home_id)
    if coord is None:
        return {"error": "No coordinator for this device's home"}

    state = (coord.data or {}).get(module.module_id)
    state_dict: dict[str, Any] = {}
    if state:
        state_dict = {
            "current_temp": state.current_temp,
            "humidity": state.humidity,
            "target_temp": state.target_temp,
            "setpoint_mode": state.setpoint_mode,
            "fan_mode": state.fan_mode,
            "fan_speed": state.fan_speed,
            "reachable": state.reachable,
        }

    coord_diag = dict(coord.diagnostics)
    coord_diag["home_id"] = _REDACTED

    return {
        "module_id": _REDACTED,
        "coordinator": coord_diag,
        "last_known_state": state_dict,
    }


# ---------------------------------------------------------------------------
# HA platform entry points (must be coroutines per platform contract)
# ---------------------------------------------------------------------------

async def async_get_config_entry_diagnostics(  # NOSONAR - HA platform contract requires async def
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    data = hass.data[DOMAIN].get(entry.entry_id, {})
    return _entry_diagnostics(entry, data.get("coordinators", {}))


async def async_get_device_diagnostics(  # NOSONAR - HA platform contract requires async def
    hass: HomeAssistant, entry: ConfigEntry, device: Any
) -> dict[str, Any]:
    data = hass.data[DOMAIN].get(entry.entry_id, {})
    return _device_diagnostics(
        entry,
        data.get("coordinators", {}),
        data.get("modules", {}),
        device,
    )
