"""DataUpdateCoordinator for Netatmo Smart AC Controller.

Implements:
- 90 s baseline polling, 9 s burst for 60 s after commands
- Exponential backoff on 429 / 5xx
- Stale / unavailable freshness tracking
- Pending-command state for post-write reconciliation
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import NetatmoApiClient, NetatmoAuthError, NetatmoRateLimitError, NacModule, NacState
from .const import (
    BURST_DURATION,
    PENDING_TIMEOUT,
    POLL_INTERVAL_BASELINE,
    POLL_INTERVAL_BURST,
    STALE_THRESHOLD,
    UNAVAILABLE_THRESHOLD,
)

_LOGGER = logging.getLogger(__name__)

# Maximum backoff delay in seconds before retrying after a server error
_MAX_BACKOFF = 300


@dataclass
class PendingCommand:
    """Tracks a write that has been sent but not yet confirmed by a poll."""
    target_mode: str | None
    target_temp: float | None
    issued_at: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.issued_at) > PENDING_TIMEOUT


class NacCoordinator(DataUpdateCoordinator[dict[str, NacState]]):
    """Coordinator for one Netatmo home.

    One coordinator per config entry × home (CONTEXT: Isolation Rule).
    Data shape: {module_id: NacState}.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: NetatmoApiClient,
        home_id: str,
        entry: ConfigEntry,
        modules: list[NacModule],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"netatmo_ac_{home_id}",
            update_interval=timedelta(seconds=POLL_INTERVAL_BASELINE),
        )
        self._client = client
        self._home_id = home_id
        self._entry = entry
        # module_id → room_id; built once from topology at setup time because
        # homestatus does not include room_id on module objects.
        self._module_room_map: dict[str, str] = {m.module_id: m.room_id for m in modules}

        # Burst tracking
        self._burst_until: float = 0.0

        # Backoff tracking
        self._backoff_until: float = 0.0
        self._consecutive_errors: int = 0

        # Pending commands keyed by module_id
        self._pending: dict[str, PendingCommand] = {}

        # Diagnostics counters
        self.total_polls: int = 0
        self.total_errors: int = 0
        self.last_error: str | None = None
        self.last_successful_fetch: float | None = None

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, NacState]:
        """Fetch live status; adapt cadence; enforce backoff."""
        now = time.monotonic()

        if now < self._backoff_until:
            remaining = self._backoff_until - now
            _LOGGER.debug("Skipping poll for %s (backoff %.0f s remaining)", self._home_id, remaining)
            if self.data:
                return self.data  # return stale data during backoff
            raise UpdateFailed(f"Waiting for backoff ({remaining:.0f} s)")

        self.total_polls += 1

        try:
            states = await self._client.async_get_nac_status(self._home_id, self._module_room_map)
        except NetatmoAuthError as err:
            raise ConfigEntryAuthFailed from err
        except NetatmoRateLimitError as err:
            self._handle_error(str(err), backoff_base=60)
            if self.data:
                return self.data
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            self._handle_error(str(err), backoff_base=15)
            if self.data:
                return self.data
            raise UpdateFailed(str(err)) from err

        # Success — reset error counters
        self._consecutive_errors = 0
        self.last_successful_fetch = now

        result = {s.module_id: s for s in states}

        # Reconcile pending commands (CONTEXT: Unknown Outcome Rule, Pending Update Rule)
        self._reconcile_pending(result)

        # Adapt cadence for next poll
        self._adapt_cadence(now)

        return result

    def _handle_error(self, msg: str, backoff_base: int) -> None:
        self._consecutive_errors += 1
        self.total_errors += 1
        self.last_error = msg
        delay = min(backoff_base * (2 ** (self._consecutive_errors - 1)), _MAX_BACKOFF)
        self._backoff_until = time.monotonic() + delay
        _LOGGER.warning(
            "Poll error for home %s (attempt %d): %s. Backing off %.0f s.",
            self._home_id, self._consecutive_errors, msg, delay,
        )

    def _adapt_cadence(self, now: float) -> None:
        """Switch between burst and baseline cadence."""
        if now < self._burst_until:
            new_interval = timedelta(seconds=POLL_INTERVAL_BURST)
        else:
            new_interval = timedelta(seconds=POLL_INTERVAL_BASELINE)

        if self.update_interval != new_interval:
            self.update_interval = new_interval

    def _reconcile_pending(self, result: dict[str, NacState]) -> None:
        for module_id, cmd in tuple(self._pending.items()):
            if cmd.expired:
                _LOGGER.warning(
                    "Command for module %s was not confirmed within %d s (CONTEXT: Unconfirmed Timeout Rule).",
                    module_id, PENDING_TIMEOUT,
                )
                del self._pending[module_id]
                continue

            state = result.get(module_id)
            if state is None:
                continue

            # Check if confirmed state matches the pending command
            mode_confirmed = (
                cmd.target_mode is None
                or (cmd.target_mode == "off" and state.setpoint_mode == "off")
                or (cmd.target_mode == "manual" and state.setpoint_mode == "manual")
            )
            temp_confirmed = (
                cmd.target_temp is None
                or (state.target_temp is not None and abs(state.target_temp - cmd.target_temp) < 0.6)
            )

            if mode_confirmed and temp_confirmed:
                _LOGGER.debug("Command for module %s confirmed by provider state.", module_id)
                del self._pending[module_id]

    # ------------------------------------------------------------------
    # Freshness helpers (used by climate entities)
    # ------------------------------------------------------------------

    def is_stale(self, module_id: str) -> bool:
        state = (self.data or {}).get(module_id)
        if state is None:
            return True
        return (time.monotonic() - state.fetched_at) > STALE_THRESHOLD

    def is_unavailable(self, module_id: str) -> bool:
        if self.last_successful_fetch is None:
            return True
        return (time.monotonic() - self.last_successful_fetch) > UNAVAILABLE_THRESHOLD

    # ------------------------------------------------------------------
    # Post-command burst trigger
    # ------------------------------------------------------------------

    def trigger_burst(self, module_id: str, mode: str, temp: float | None) -> None:
        """Start burst cadence and register a pending command."""
        self._burst_until = time.monotonic() + BURST_DURATION
        self._pending[module_id] = PendingCommand(target_mode=mode, target_temp=temp)
        self.update_interval = timedelta(seconds=POLL_INTERVAL_BURST)
        _LOGGER.debug(
            "Burst mode activated for home %s after command on module %s.",
            self._home_id, module_id,
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def diagnostics(self) -> dict[str, Any]:
        now = time.monotonic()
        age = (now - self.last_successful_fetch) if self.last_successful_fetch else None
        return {
            "home_id": self._home_id,
            "total_polls": self.total_polls,
            "total_errors": self.total_errors,
            "last_error": self.last_error,
            "state_age_seconds": round(age, 1) if age is not None else None,
            "is_stale": age is not None and age > STALE_THRESHOLD,
            "backoff_remaining_seconds": max(0, round(self._backoff_until - now, 1)),
            "burst_active": now < self._burst_until,
            "pending_commands": list(self._pending.keys()),
            "requests_last_hour": self._client.rate_limiter.requests_last_hour,
        }
