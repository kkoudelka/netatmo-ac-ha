"""Netatmo Cloud API client for Smart AC Controller (NAC)."""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from .const import (
    MODULE_TYPE_NAC,
    NETATMO_API_BASE,
    NETATMO_SYNC_API_BASE,
    RATE_LIMIT_PER_10S,
    RATE_LIMIT_PER_HOUR,
    RATE_POLLING_HEADROOM,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class NetatmoApiError(Exception):
    """Generic API error."""


class NetatmoAuthError(NetatmoApiError):
    """Raised on 401/403 — triggers config-entry reauth."""


class NetatmoRateLimitError(NetatmoApiError):
    """Raised on 429 — caller should back off."""


# Provider fan_mode string → numeric fan_speed (observed from devtools: 1=low, 2=medium, 3=high)
NETATMO_FAN_MODE_TO_SPEED: dict[str, int] = {"low": 1, "medium": 2, "high": 3}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class HomeInfo:
    home_id: str
    name: str


@dataclass
class NacModule:
    module_id: str
    room_id: str
    home_id: str
    name: str
    temp_min: float = 16.0
    temp_max: float = 32.0
    temp_step: float = 1.0
    fan_speed_min: int = 1
    fan_speed_max: int = 3
    fan_speed_step: int = 1


@dataclass
class NacState:
    module_id: str
    room_id: str
    # Confirmed sensor readings
    current_temp: float | None = None
    humidity: int | None = None
    # Confirmed setpoint state
    target_temp: float | None = None
    setpoint_mode: str | None = None   # "manual" | "schedule" | "off"
    setpoint_end_time: int | None = None
    # Confirmed fan state
    fan_mode: str | None = None        # "auto" | "low" | "medium" | "high"
    fan_speed: int | None = None       # numeric speed; secondary to fan_mode
    # Reachability
    reachable: bool = True
    # Monotonic timestamp of when this state was populated
    fetched_at: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Client-side sliding-window rate limiter.

    Tracks two windows (10 s and 1 h) and enforces conservative budgets so
    that polling never consumes the headroom reserved for user commands.
    """

    def __init__(self) -> None:
        self._budget_10s = int(RATE_LIMIT_PER_10S * RATE_POLLING_HEADROOM)
        self._budget_hour = int(RATE_LIMIT_PER_HOUR * RATE_POLLING_HEADROOM)
        self._ts_10s: deque[float] = deque()
        self._ts_hour: deque[float] = deque()

    def consume(self) -> bool:
        """Return True and record the request, or False if budget is exhausted."""
        now = time.monotonic()
        cutoff_10s = now - 10.0
        cutoff_hour = now - 3600.0

        while self._ts_10s and self._ts_10s[0] < cutoff_10s:
            self._ts_10s.popleft()
        while self._ts_hour and self._ts_hour[0] < cutoff_hour:
            self._ts_hour.popleft()

        if len(self._ts_10s) >= self._budget_10s:
            return False
        if len(self._ts_hour) >= self._budget_hour:
            return False

        self._ts_10s.append(now)
        self._ts_hour.append(now)
        return True

    @property
    def requests_last_hour(self) -> int:
        now = time.monotonic()
        cutoff = now - 3600.0
        while self._ts_hour and self._ts_hour[0] < cutoff:
            self._ts_hour.popleft()
        return len(self._ts_hour)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class NetatmoApiClient:
    """Thin async wrapper around Netatmo Cloud REST APIs.

    All state-mutating methods are at-most-once: they do not retry on
    uncertain outcomes (CONTEXT: Write Reliability Model, Unknown Outcome Rule).
    """

    def __init__(self, session: OAuth2Session) -> None:
        self._session = session
        self.rate_limiter = _RateLimiter()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        base_url: str | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        _bypass_rate_limit: bool = False,
    ) -> dict[str, Any]:
        if not _bypass_rate_limit and not self.rate_limiter.consume():
            raise NetatmoRateLimitError("Client-side rate budget exhausted")

        url = f"{base_url or NETATMO_API_BASE}{path}"
        try:
            resp: aiohttp.ClientResponse = await self._session.async_request(
                method,
                url,
                params=params,
                json=json,
            )
        except aiohttp.ClientConnectionError as err:
            raise NetatmoApiError(f"Connection error: {err}") from err

        if resp.status in (401, 403):
            raise NetatmoAuthError(f"Auth error ({resp.status})")
        if resp.status == 429:
            raise NetatmoRateLimitError("Netatmo rate limit (429)")
        if resp.status >= 500:
            raise NetatmoApiError(f"Server error ({resp.status})")
        if resp.status >= 400:
            text = await resp.text()
            raise NetatmoApiError(f"API error {resp.status}: {text}")

        return await resp.json()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def async_get_homes(self) -> list[HomeInfo]:
        data = await self._request("GET", "/homesdata")
        return [
            HomeInfo(home_id=h["id"], name=h.get("name", h["id"]))
            for h in data.get("body", {}).get("homes", [])
            if "id" in h
        ]

    async def async_get_nac_modules(self, home_id: str) -> list[NacModule]:
        """Discover NAC modules for a home using homesdata topology.

        homesdata?home_id=X includes NAC modules with room_id directly on the
        module object, and room names in the rooms array — one API call is enough.
        homestatus is reserved for runtime state polling.
        """
        data = await self._request("GET", "/homesdata", params={"home_id": home_id})
        home = next(
            (h for h in data.get("body", {}).get("homes", []) if h.get("id") == home_id),
            None,
        )
        if not home:
            return []

        rooms_by_id = {r["id"]: r for r in home.get("rooms", [])}

        # Fetch per-device capability specs (CONTEXT: Capability Discovery Rule)
        specs_by_module = await self._get_nac_specs(home_id)

        modules: list[NacModule] = []
        for mod in home.get("modules", []):
            if mod.get("type") != MODULE_TYPE_NAC:
                continue  # CONTEXT: Non-AC Exclusion Rule

            module_id = mod.get("id")
            if not module_id:
                continue

            room_id = mod.get("room_id", "")
            room = rooms_by_id.get(room_id, {})
            name = room.get("name") or mod.get("name", module_id)
            specs = specs_by_module.get(module_id, {})

            temp_spec = specs.get("cooling_setpoint_temperature", {})
            fan_spec = specs.get("cooling_fan_speed", {})

            modules.append(NacModule(
                module_id=module_id,
                room_id=room_id,
                home_id=home_id,
                name=name,
                temp_min=float(temp_spec.get("min", 16.0)),
                temp_max=float(temp_spec.get("max", 32.0)),
                temp_step=float(temp_spec.get("step", 1.0)),
                fan_speed_min=int(fan_spec.get("min", 1)),
                fan_speed_max=int(fan_spec.get("max", 3)),
                fan_speed_step=int(fan_spec.get("step", 1)),
            ))

        return modules

    async def _get_nac_specs(self, home_id: str) -> dict[str, dict]:
        """Return specifications keyed by module_id from getconfigs.

        Uses syncapi/v1/getconfigs (validated from devtools, 2026-06-18).
        Falls back to empty dict on failure so discovery still succeeds.
        """
        import json as _json
        try:
            data = await self._request(
                "GET", "/getconfigs",
                base_url=NETATMO_SYNC_API_BASE,
                params={"home_id": home_id, "device_types": _json.dumps([MODULE_TYPE_NAC])},
            )
        except NetatmoApiError:
            _LOGGER.warning("getconfigs failed for home %s; using default capability bounds", home_id)
            return {}

        result: dict[str, dict] = {}
        for mod in data.get("body", {}).get("home", {}).get("modules", []):
            module_id = mod.get("id")
            if module_id:
                result[module_id] = mod.get("specifications", {})
        return result

    # ------------------------------------------------------------------
    # Runtime state
    # ------------------------------------------------------------------

    async def async_get_nac_status(
        self,
        home_id: str,
        module_room_map: dict[str, str],
    ) -> list[NacState]:
        """Return live runtime state for all NAC modules in a home.

        module_room_map: {module_id: room_id} — built from topology at setup
        time and passed in because homestatus does not include room_id on modules.

        Field notes from live API (validated 2026-06-18):
        - reachable is on the room, not the module
        - cooling_setpoint_end_time (not endtime) on the room
        """
        data = await self._request(
            "GET",
            "/homestatus",
            params={"home_id": home_id, "device_types": MODULE_TYPE_NAC},
        )
        home = data.get("body", {}).get("home", {})

        rooms_by_id: dict[str, dict] = {r["id"]: r for r in home.get("rooms", [])}
        states: list[NacState] = []

        for mod in home.get("modules", []):
            if mod.get("type") != MODULE_TYPE_NAC:
                continue

            module_id = mod.get("id")
            if not module_id:
                continue

            room_id = module_room_map.get(module_id, "")
            room = rooms_by_id.get(room_id, {})

            states.append(NacState(
                module_id=module_id,
                room_id=room_id,
                current_temp=room.get("therm_measured_temperature"),
                humidity=room.get("humidity"),
                target_temp=room.get("cooling_setpoint_temperature"),
                setpoint_mode=room.get("cooling_setpoint_mode"),
                setpoint_end_time=room.get("cooling_setpoint_end_time"),
                fan_mode=mod.get("fan_mode"),
                fan_speed=mod.get("fan_speed"),
                reachable=room.get("reachable", False),
            ))

        return states

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    async def async_set_cool_setpoint(
        self,
        home_id: str,
        room_id: str,
        mode: str,
        temp: float | None = None,
        endtime: int | None = None,
    ) -> None:
        """Send a cooling setpoint command.

        Uses the web-app syncapi endpoint (validated from devtools, 2026-06-18).
        mode: "manual" with temp+endtime for a timed override, "off" to power off.
        At-most-once: not retried on uncertain outcome
        (CONTEXT: Write Reliability Model, Unknown Outcome Rule).
        """
        room: dict[str, Any] = {"id": room_id, "cooling_setpoint_mode": mode}
        if temp is not None:
            room["cooling_setpoint_temperature"] = temp
        if endtime is not None:
            room["cooling_setpoint_end_time"] = endtime

        payload = {"home": {"id": home_id, "rooms": [room]}}
        await self._request(
            "POST", "/setstate",
            base_url=NETATMO_SYNC_API_BASE,
            json=payload,
            _bypass_rate_limit=True,
        )
        _LOGGER.debug("setstate (cooling) sent: home=%s room=%s mode=%s temp=%s", home_id, room_id, mode, temp)

    async def async_set_fan_speed(
        self,
        home_id: str,
        module_id: str,
        fan_speed: int,
        endtime: int | None = None,
    ) -> None:
        """Send a fan speed command.

        fan_speed: 1=low, 2=medium, 3=high (observed from devtools).
        At-most-once (CONTEXT: Write Reliability Model).
        """
        module: dict[str, Any] = {
            "id": module_id,
            "fan_mode": "manual",
            "fan_speed": fan_speed,
            "fan_setpoint_from": "module",
        }
        if endtime is not None:
            module["fan_end_time"] = endtime

        payload = {"home": {"id": home_id, "modules": [module]}}
        await self._request(
            "POST", "/setstate",
            base_url=NETATMO_SYNC_API_BASE,
            json=payload,
            _bypass_rate_limit=True,
        )
        _LOGGER.debug("setstate (fan) sent: home=%s module=%s speed=%s", home_id, module_id, fan_speed)
