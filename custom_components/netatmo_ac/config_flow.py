"""Config flow for Netatmo Smart AC Controller.

Flow steps:
  1. pick_implementation  (OAuth2 — handled by HA framework)
  2. OAuth2 authorisation (handled by HA framework)
  3. select_homes         (multi-select; all pre-checked)
  4. select_modules       (multi-select; all NAC devices pre-checked)
  5. override_duration    (settings; defaults to 60 min)

Re-auth flow re-runs OAuth2 only and preserves existing home/module selection.

CONTEXT: Onboarding Selection Flow, Onboarding Default Rule, Reauth Expectation.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_entry_oauth2_flow, selector

from .api import NetatmoApiClient, NetatmoApiError, NetatmoAuthError
from .const import (
    CONF_HOME_IDS,
    CONF_MODULE_IDS,
    CONF_MODULE_NAMES,
    CONF_OVERRIDE_DURATION,
    CONF_TEMP_SENSORS,
    DEFAULT_OVERRIDE_DURATION,
    DEFAULT_OVERRIDE_DURATION_MINUTES,
    DOMAIN,
    OAUTH2_SCOPES,
)

_LOGGER = logging.getLogger(__name__)


class OAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler,
    domain=DOMAIN,
):
    """Config flow handler with OAuth2 + home/device selection."""

    DOMAIN = DOMAIN

    def __init__(self) -> None:
        super().__init__()
        self._oauth_data: dict[str, Any] = {}
        self._available_homes: list[dict[str, str]] = []    # [{value, label}]
        self._available_modules: list[dict[str, str]] = []  # [{value, label}]
        self._selected_home_ids: list[str] = []
        self._module_names: dict[str, str] = {}             # module_id → display name

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> NetatmoAcOptionsFlow:
        return NetatmoAcOptionsFlow(config_entry)

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        return {"scope": " ".join(OAUTH2_SCOPES)}

    # ------------------------------------------------------------------
    # Step: select homes
    # ------------------------------------------------------------------

    async def async_step_select_homes(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            selected = user_input.get(CONF_HOME_IDS, [])
            if not selected:
                return self.async_show_form(
                    step_id="select_homes",
                    data_schema=self._homes_schema(self._available_homes),
                    errors={"base": "no_nac_devices"},
                )
            self._selected_home_ids = selected
            return await self.async_step_select_modules()

        # Fetch homes via temporary session
        try:
            client = await self._build_client()
            homes = await client.async_get_homes()
        except NetatmoAuthError:
            return self.async_abort(reason="oauth_error")
        except NetatmoApiError:
            _LOGGER.exception("Failed to fetch homes")
            return self.async_abort(reason="cannot_connect")

        if not homes:
            _LOGGER.error("No homes returned from /api/homesdata — check Netatmo app scopes")
            return self.async_abort(reason="no_nac_devices")

        self._available_homes = [{"value": h.home_id, "label": h.name} for h in homes]

        return self.async_show_form(
            step_id="select_homes",
            data_schema=self._homes_schema(self._available_homes),
        )

    # ------------------------------------------------------------------
    # Step: select modules (NAC devices)
    # ------------------------------------------------------------------

    async def async_step_select_modules(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            selected = user_input.get(CONF_MODULE_IDS, [])
            if not selected:
                return self.async_show_form(
                    step_id="select_modules",
                    data_schema=self._modules_schema(self._available_modules),
                    errors={"base": "no_nac_devices"},
                )
            return await self.async_step_override_duration(
                prefill={CONF_MODULE_IDS: selected}
            )

        # Discover NAC modules across selected homes
        try:
            client = await self._build_client()
            modules = []
            for home_id in self._selected_home_ids:
                modules.extend(await client.async_get_nac_modules(home_id))
        except NetatmoApiError:
            _LOGGER.exception("Failed to fetch NAC modules")
            return self.async_abort(reason="cannot_connect")

        if not modules:
            _LOGGER.error("No NAC modules found in homes %s", self._selected_home_ids)
            return self.async_abort(reason="no_nac_devices")

        self._available_modules = [
            {"value": m.module_id, "label": f"{m.name} ({m.home_id[:8]}…)"}
            for m in modules
        ]
        self._module_names = {m.module_id: m.name for m in modules}

        return self.async_show_form(
            step_id="select_modules",
            data_schema=self._modules_schema(self._available_modules),
        )

    # ------------------------------------------------------------------
    # Step: override duration setting
    # ------------------------------------------------------------------

    async def async_step_override_duration(
        self,
        user_input: dict[str, Any] | None = None,
        prefill: dict[str, Any] | None = None,
    ) -> FlowResult:
        if not hasattr(self, "_prefill"):
            self._prefill = prefill or {}
        elif prefill:
            self._prefill = prefill

        if user_input is not None:
            duration_min = int(user_input[CONF_OVERRIDE_DURATION])
            entry_data = {
                **self._oauth_data,
                CONF_HOME_IDS: self._selected_home_ids,
                CONF_MODULE_IDS: self._prefill.get(CONF_MODULE_IDS, []),
                CONF_MODULE_NAMES: self._module_names,
                CONF_OVERRIDE_DURATION: duration_min * 60,
            }
            return self.async_create_entry(title=self._entry_title(), data=entry_data)

        schema = vol.Schema({
            vol.Required(
                CONF_OVERRIDE_DURATION,
                default=DEFAULT_OVERRIDE_DURATION_MINUTES,
            ): selector.selector({
                "number": {
                    "min": 5,
                    "max": 480,
                    "step": 5,
                    "mode": "box",
                    "unit_of_measurement": "min",
                }
            }),
        })

        return self.async_show_form(step_id="override_duration", data_schema=schema)

    # ------------------------------------------------------------------
    # Reauth
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> FlowResult:  # type: ignore[override]
        """Called after a successful OAuth2 round-trip.

        For initial setup: proceed to home selection.
        For reauth: update the existing entry's token and finish.
        """
        existing_entry = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, self.unique_id
        )
        if existing_entry is not None:
            # Reauth path — update token, preserve config
            self.hass.config_entries.async_update_entry(
                existing_entry, data={**existing_entry.data, **data}
            )
            await self.hass.config_entries.async_reload(existing_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        # Initial setup path
        self._oauth_data = data
        return await self.async_step_select_homes()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _build_client(self) -> NetatmoApiClient:
        """Build a discovery client using the freshly acquired OAuth2 token.

        Uses self.flow_impl (set by AbstractOAuth2FlowHandler) and a lightweight
        fake entry carrying only the token dict. Token refresh is not needed here
        because the token was just issued; if it has already expired the flow
        will surface a connection error and the user must restart setup.
        """
        session = config_entry_oauth2_flow.OAuth2Session(
            self.hass,
            type("_FlowTokenEntry", (), {"data": self._oauth_data})(),  # type: ignore[arg-type]
            self.flow_impl,
        )
        return NetatmoApiClient(session)

    def _homes_schema(self, homes: list[dict[str, str]]) -> vol.Schema:
        return vol.Schema({
            vol.Required(CONF_HOME_IDS, default=[h["value"] for h in homes]): selector.selector({
                "select": {
                    "options": homes,
                    "multiple": True,
                }
            }),
        })

    def _modules_schema(self, modules: list[dict[str, str]]) -> vol.Schema:
        return vol.Schema({
            vol.Required(CONF_MODULE_IDS, default=[m["value"] for m in modules]): selector.selector({
                "select": {
                    "options": modules,
                    "multiple": True,
                }
            }),
        })

    def _entry_title(self) -> str:
        if self._available_homes:
            names = [h["label"] for h in self._available_homes if h["value"] in self._selected_home_ids]
            return f"Netatmo AC – {', '.join(names)}" if names else "Netatmo Smart AC Controller"
        return "Netatmo Smart AC Controller"


# ---------------------------------------------------------------------------
# Options flow (reconfiguration)
# ---------------------------------------------------------------------------

class NetatmoAcOptionsFlow(OptionsFlow):
    """Options: override duration + per-module temperature sensor override."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        module_ids: list[str] = self._entry.data.get(CONF_MODULE_IDS, [])
        module_names: dict[str, str] = self._entry.data.get(CONF_MODULE_NAMES, {})
        current_opts = self._entry.options
        current_sensors: dict[str, list[str]] = current_opts.get(CONF_TEMP_SENSORS, {})

        # Override duration: prefer options (user-updated), fall back to original data
        current_minutes = (
            current_opts.get(CONF_OVERRIDE_DURATION)
            or self._entry.data.get(CONF_OVERRIDE_DURATION, DEFAULT_OVERRIDE_DURATION)
        ) // 60

        if user_input is not None:
            duration_min = int(user_input[CONF_OVERRIDE_DURATION])
            new_sensors: dict[str, list[str]] = {}
            for i, module_id in enumerate(module_ids):
                val = user_input.get(f"temp_sensor_{i}", [])
                if val:
                    new_sensors[module_id] = val if isinstance(val, list) else [val]
            return self.async_create_entry(data={
                CONF_OVERRIDE_DURATION: duration_min * 60,
                CONF_TEMP_SENSORS: new_sensors,
            })

        schema_dict: dict = {
            vol.Required(CONF_OVERRIDE_DURATION, default=current_minutes): selector.selector({
                "number": {
                    "min": 5,
                    "max": 480,
                    "step": 5,
                    "mode": "box",
                    "unit_of_measurement": "min",
                }
            }),
        }
        for i, module_id in enumerate(module_ids):
            schema_dict[vol.Optional(f"temp_sensor_{i}", default=current_sensors.get(module_id, []))] = (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        device_class="temperature",
                        multiple=True,
                    )
                )
            )

        display_names = [module_names.get(mid, mid) for mid in module_ids]
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"module_names": ", ".join(display_names)},
        )
