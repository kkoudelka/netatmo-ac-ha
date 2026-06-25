"""Climate entity for Netatmo Smart AC Controller (NAC module).

State mapping:
  setpoint_mode "off"      → HVACMode.OFF
  setpoint_mode "manual"   → HVACMode.COOL
  setpoint_mode "schedule" → HVACMode.COOL  (schedule is running; HA still shows COOL)

Fan mode mapping (Netatmo → HA):
  "auto"   → FAN_AUTO
  "low"    → FAN_LOW
  "medium" → FAN_MEDIUM
  "high"   → FAN_HIGH
  numeric fan_speed is used as fallback if fan_mode is absent.

Conservative mode advertisement: only OFF + COOL by default
(CONTEXT: Conservative Mode Advertisement Rule, Progressive Capability Expansion Rule).
"""
from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import (
    ATTR_FAN_MODE,
    ATTR_HVAC_MODE,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import NacModule, NacState, NetatmoApiClient, NetatmoApiError, NetatmoAuthError
from .const import (
    ATTR_DURATION,
    CONF_MODULE_IDS,
    CONF_OVERRIDE_DURATION,
    CONF_TEMP_SENSORS,
    DEFAULT_OVERRIDE_DURATION,
    DOMAIN,
    ENTITY_PICTURE_URL,
    MAX_OVERRIDE_DURATION_MINUTES,
    MIN_OVERRIDE_DURATION_MINUTES,
    PENDING_TIMEOUT,
    SERVICE_SET_STATE,
    STALE_THRESHOLD,
    UNAVAILABLE_THRESHOLD,
)
from .coordinator import NacCoordinator

_LOGGER = logging.getLogger(__name__)

# Netatmo fan_mode string → HA fan mode constant
_FAN_MODE_MAP: dict[str, str] = {
    "low": FAN_LOW,
    "medium": FAN_MEDIUM,
    "high": FAN_HIGH,
}
_FAN_SPEED_MAP: dict[int, str] = {1: FAN_LOW, 2: FAN_MEDIUM, 3: FAN_HIGH}

# HA fan mode → Netatmo fan_speed (validated from devtools: 1=low, 2=medium, 3=high)
_HA_FAN_TO_SPEED: dict[str, int] = {
    FAN_LOW: 1,
    FAN_MEDIUM: 2,
    FAN_HIGH: 3,
}

# Combined override service schema (CONTEXT: Combined Override Service Exception)
# Must be built via cv.make_entity_service_schema so entity_platform recognizes it
# as an entity service schema (it stamps a marker checked by is_entity_service_schema).
SET_STATE_SCHEMA = vol.All(
    cv.make_entity_service_schema({
        vol.Optional(ATTR_HVAC_MODE): vol.In([HVACMode.OFF, HVACMode.COOL]),
        vol.Optional(ATTR_TEMPERATURE): vol.Coerce(float),
        vol.Optional(ATTR_FAN_MODE): vol.In([FAN_LOW, FAN_MEDIUM, FAN_HIGH]),
        vol.Optional(ATTR_DURATION): vol.All(
            vol.Coerce(int),
            vol.Range(min=MIN_OVERRIDE_DURATION_MINUTES, max=MAX_OVERRIDE_DURATION_MINUTES),
        ),
    }),
    cv.has_at_least_one_key(ATTR_HVAC_MODE, ATTR_TEMPERATURE, ATTR_FAN_MODE),
)


async def async_setup_entry(  # NOSONAR - HA platform contract requires async def
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    coordinators: dict[str, NacCoordinator] = data["coordinators"]
    modules: dict[str, NacModule] = data["modules"]       # module_id → NacModule
    client: NetatmoApiClient = data["client"]

    allowed_module_ids: set[str] = set(entry.data.get(CONF_MODULE_IDS, []))

    entities = []
    for module_id, module in modules.items():
        if module_id not in allowed_module_ids:
            continue
        coordinator = coordinators.get(module.home_id)
        if coordinator is None:
            continue
        entities.append(
            NetatmoAcClimate(
                coordinator=coordinator,
                module=module,
                client=client,
                entry=entry,
            )
        )

    async_add_entities(entities)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_STATE,
        SET_STATE_SCHEMA,
        "async_netatmo_set_state",
    )


class NetatmoAcClimate(CoordinatorEntity[NacCoordinator], ClimateEntity):
    """Climate entity backed by one NAC module.

    Identity: derived from stable provider IDs + config entry context
    (CONTEXT: Identity Stability Rule, Unique Identity Composition).
    """

    _attr_has_entity_name = True
    _attr_name = None  # use device name as entity name
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_entity_picture = ENTITY_PICTURE_URL

    # Conservative advertisement: off + cool only by default
    # (CONTEXT: Conservative Mode Advertisement Rule)
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL]

    def __init__(
        self,
        coordinator: NacCoordinator,
        module: NacModule,
        client: NetatmoApiClient,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._module = module
        self._client = client
        self._entry = entry

        # Stable unique_id from provider identifiers (CONTEXT: Unique Identity Composition)
        self._attr_unique_id = f"{module.home_id}_{module.module_id}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, module.module_id)},
            name=module.name,
            manufacturer="Netatmo",
            model="Smart AC Controller",
        )

        # Temperature constraints from capability discovery (CONTEXT: Capability Discovery Rule)
        self._attr_min_temp = module.temp_min
        self._attr_max_temp = module.temp_max
        self._attr_target_temperature_step = module.temp_step

        # Fan modes from config — build from speed range (e.g. 1-3 → LOW/MEDIUM/HIGH)
        self._attr_fan_modes = [
            _FAN_SPEED_MAP[s]
            for s in range(module.fan_speed_min, module.fan_speed_max + 1, module.fan_speed_step)
            if s in _FAN_SPEED_MAP
        ] or None

    # ------------------------------------------------------------------
    # Coordinator update
    # ------------------------------------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()

    @property
    def _override_duration(self) -> int:
        """Override duration in seconds; options take precedence over initial data."""
        return (
            self._entry.options.get(CONF_OVERRIDE_DURATION)
            or self._entry.data.get(CONF_OVERRIDE_DURATION, DEFAULT_OVERRIDE_DURATION)
        )

    # ------------------------------------------------------------------
    # Availability (CONTEXT: Unavailability Threshold Rule, Degradation Rule)
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        if self.coordinator.is_unavailable(self._module.module_id):
            return False
        state = self._current_nac_state
        if state is not None and not state.reachable:
            return False
        return True

    # ------------------------------------------------------------------
    # State properties
    # ------------------------------------------------------------------

    @property
    def _current_nac_state(self) -> NacState | None:
        return (self.coordinator.data or {}).get(self._module.module_id)

    @property
    def hvac_mode(self) -> HVACMode:
        state = self._current_nac_state
        if state is None:
            return HVACMode.OFF
        mode = state.setpoint_mode
        if mode == "off" or mode is None:
            return HVACMode.OFF
        return HVACMode.COOL  # manual and schedule both map to COOL

    @property
    def hvac_action(self) -> HVACAction | None:
        state = self._current_nac_state
        if state is None:
            return None
        if state.setpoint_mode == "off":
            return HVACAction.OFF
        if state.current_temp is not None and state.target_temp is not None:
            if state.current_temp > state.target_temp:
                return HVACAction.COOLING
            return HVACAction.IDLE
        return HVACAction.IDLE

    @property
    def current_temperature(self) -> float | None:
        configured: list[str] = (
            self._entry.options.get(CONF_TEMP_SENSORS, {}).get(self._module.module_id, [])
        )
        if configured:
            readings: list[float] = []
            for entity_id in configured:
                state = self.hass.states.get(entity_id)
                if state and state.state not in ("unknown", "unavailable"):
                    try:
                        readings.append(float(state.state))
                    except ValueError:
                        pass
            if readings:
                return round(sum(readings) / len(readings), 1)
        # Fall back to NAC's own sensor
        nac_state = self._current_nac_state
        return nac_state.current_temp if nac_state else None

    @property
    def target_temperature(self) -> float | None:
        state = self._current_nac_state
        return state.target_temp if state else None

    @property
    def current_humidity(self) -> int | None:
        state = self._current_nac_state
        return state.humidity if state else None

    @property
    def fan_mode(self) -> str | None:
        state = self._current_nac_state
        if state is None:
            return None
        if state.fan_mode:
            return _FAN_MODE_MAP.get(state.fan_mode, state.fan_mode)
        if state.fan_speed is not None:
            return _FAN_SPEED_MAP.get(state.fan_speed)
        return None

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )
        if self._attr_fan_modes:
            features |= ClimateEntityFeature.FAN_MODE
        return features

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        state = self._current_nac_state
        if state is None:
            return attrs
        if state.setpoint_mode:
            attrs["netatmo_setpoint_mode"] = state.setpoint_mode
        if state.setpoint_end_time:
            attrs["netatmo_setpoint_end_time"] = state.setpoint_end_time
        # Surface pending state for diagnostics without using it as truth
        # (CONTEXT: Pending Update Rule, Consistency Priority)
        if self._module.module_id in self.coordinator._pending:
            attrs["command_pending"] = True
        is_stale = self.coordinator.is_stale(self._module.module_id)
        if is_stale:
            attrs["state_stale"] = True
        return attrs

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self._send_command(mode="off", temp=None)
        elif hvac_mode == HVACMode.COOL:
            # When switching to COOL without a target temp, use current target or midpoint
            target = self.target_temperature or ((self._module.temp_min + self._module.temp_max) / 2)
            await self._send_command(mode="manual", temp=target)
        else:
            # Unsupported mode — explicit failure (CONTEXT: Unsupported Command Rule)
            raise ServiceValidationError(
                f"HVAC mode {hvac_mode} is not supported by this AC unit. "
                f"Supported modes: {self._attr_hvac_modes}"
            )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        await self._send_command(mode="manual", temp=self._validate_temperature(temp))

    def _validate_temperature(self, temp: float) -> float:
        """Snap and validate a requested temperature (CONTEXT: Range Validation Rule, Step Validation Rule)."""
        if not (self._module.temp_min <= temp <= self._module.temp_max):
            raise ServiceValidationError(
                f"Temperature {temp} °C is outside the supported range "
                f"[{self._module.temp_min}, {self._module.temp_max}]."
            )
        step = self._module.temp_step
        snapped = round(round(temp / step) * step, 1)
        if abs(snapped - temp) > 0.05:
            raise ServiceValidationError(
                f"Temperature {temp} °C does not align with step {step} °C. "
                f"Nearest valid value: {snapped} °C."
            )
        return snapped

    def _validate_fan_speed(self, fan_mode: str) -> int:
        """Validate a requested fan mode and return its Netatmo speed (CONTEXT: Unsupported Command Rule)."""
        if fan_mode not in (self._attr_fan_modes or []):
            raise ServiceValidationError(
                f"Fan mode '{fan_mode}' is not supported. Supported: {self._attr_fan_modes}"
            )
        speed = _HA_FAN_TO_SPEED.get(fan_mode)
        if speed is None:
            raise ServiceValidationError(f"Fan mode '{fan_mode}' has no known speed mapping.")
        return speed

    async def async_set_fan_mode(self, fan_mode: str) -> None:  # NOSONAR - HA climate interface requires async def
        speed = self._validate_fan_speed(fan_mode)
        endtime = int(time.time()) + self._override_duration
        try:
            await self._client.async_set_fan_speed(
                home_id=self._module.home_id,
                module_id=self._module.module_id,
                fan_speed=speed,
                endtime=endtime,
            )
        except NetatmoAuthError as err:
            raise HomeAssistantError("Netatmo authentication failed.") from err
        except NetatmoApiError as err:
            raise HomeAssistantError(f"Fan command failed: {err}") from err

        self.coordinator.trigger_burst(self._module.module_id, mode=None, temp=None, fan_speed=speed)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        target = self.target_temperature or ((self._module.temp_min + self._module.temp_max) / 2)
        await self._send_command(mode="manual", temp=target)

    async def async_turn_off(self) -> None:
        await self._send_command(mode="off", temp=None)

    async def async_netatmo_set_state(
        self,
        hvac_mode: str | None = None,
        temperature: float | None = None,
        fan_mode: str | None = None,
        duration: int | None = None,
    ) -> None:
        """Combined override: mode, temperature, fan, and a custom duration in one call.

        CONTEXT: Combined Override Service Exception, Shared Override Duration Rule,
        Off-Combination Validation Rule, Sequential Write Composition Rule.
        """
        if hvac_mode == HVACMode.OFF and (temperature is not None or fan_mode is not None):
            raise ServiceValidationError(
                "hvac_mode 'off' cannot be combined with temperature or fan_mode."
            )

        fan_speed = self._validate_fan_speed(fan_mode) if fan_mode is not None else None
        cool_mode, cool_temp = self._resolve_cool_command(hvac_mode, temperature)
        endtime = int(time.time()) + (duration * 60 if duration is not None else self._override_duration)

        try:
            if cool_mode is not None:
                await self._client.async_set_cool_setpoint(
                    home_id=self._module.home_id,
                    room_id=self._module.room_id,
                    mode=cool_mode,
                    temp=cool_temp,
                    endtime=endtime if cool_mode == "manual" else None,
                )
            if fan_speed is not None:
                await self._client.async_set_fan_speed(
                    home_id=self._module.home_id,
                    module_id=self._module.module_id,
                    fan_speed=fan_speed,
                    endtime=endtime,
                )
        except NetatmoAuthError as err:
            raise HomeAssistantError("Netatmo authentication failed. Please re-link the integration.") from err
        except NetatmoApiError as err:
            raise HomeAssistantError(f"Netatmo command failed: {err}") from err

        self.coordinator.trigger_burst(
            self._module.module_id, mode=cool_mode, temp=cool_temp, fan_speed=fan_speed
        )
        await self.coordinator.async_request_refresh()

    def _resolve_cool_command(
        self, hvac_mode: str | None, temperature: float | None
    ) -> tuple[str | None, float | None]:
        """Resolve the requested cooling mode/temperature, or (None, None) if unchanged."""
        if hvac_mode == HVACMode.OFF:
            return "off", None
        if hvac_mode == HVACMode.COOL or temperature is not None:
            if temperature is not None:
                return "manual", self._validate_temperature(temperature)
            target = self.target_temperature or ((self._module.temp_min + self._module.temp_max) / 2)
            return "manual", target
        return None, None

    # ------------------------------------------------------------------
    # Internal command sender
    # ------------------------------------------------------------------

    async def _send_command(self, mode: str, temp: float | None) -> None:
        endtime: int | None = None
        if mode == "manual":
            endtime = int(time.time()) + self._override_duration

        try:
            await self._client.async_set_cool_setpoint(
                home_id=self._module.home_id,
                room_id=self._module.room_id,
                mode=mode,
                temp=temp,
                endtime=endtime,
            )
        except NetatmoAuthError as err:
            raise HomeAssistantError("Netatmo authentication failed. Please re-link the integration.") from err
        except NetatmoApiError as err:
            raise HomeAssistantError(f"Netatmo command failed: {err}") from err

        # Trigger burst polling and register pending confirmation
        # (CONTEXT: Post-Command Burst Cadence, Pending Update Rule)
        self.coordinator.trigger_burst(self._module.module_id, mode=mode, temp=temp)
        await self.coordinator.async_request_refresh()

