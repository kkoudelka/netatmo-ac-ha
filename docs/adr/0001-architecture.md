# ADR-0001 — Integration Architecture

**Status:** Accepted  
**Date:** 2026-06-18  
**Glossary:** All capitalised terms are defined in [`CONTEXT.md`](../../CONTEXT.md).

---

## Context

The Netatmo Smart AC Controller (NAC) is a cloud-dependent IR retrofit device.
There is no local API; all control and state go through Netatmo Cloud.
A single Home Assistant instance may have multiple Netatmo accounts, each with
multiple homes, each home containing a mix of product types (weather, camera, AC).
The primary automation surface is the HA climate entity.

---

## Decisions

### 1. One config entry per Netatmo account

Satisfies the Config Entry Boundary and Isolation Rule: tokens, refresh lifecycle,
coordinators, and entities are fully isolated per account. Multiple entries coexist
without interference.

### 2. One DataUpdateCoordinator per home

```
ConfigEntry (account)
└── NacCoordinator  (home A)  ──polls──▶  /api/homestatus?home_id=A&device_types=NAC
└── NacCoordinator  (home B)  ──polls──▶  /api/homestatus?home_id=B&device_types=NAC
```

A single coordinator per home batches all NAC module status into one API call
(NAC Status Endpoint Pattern). Entities for that home subscribe to the same
coordinator, so one poll updates all climate entities in the home simultaneously.
Coordinators for different homes are independent; a failure in one does not
affect the other.

### 3. One climate entity per NAC module

Satisfies the Entity Mapping Model and AC Entity Eligibility Rule. Non-NAC modules
in the same home are ignored (Non-AC Exclusion Rule). Entity unique identity is
`{home_id}_{module_id}`, satisfying the Unique Identity Composition and
Name Independence Rules.

### 4. Adaptive polling cadence

| Phase | Interval | Trigger |
|---|---|---|
| Baseline | 90 s | steady state |
| Burst | 9 s | any user command, for 60 s |
| Decay | back to 90 s | 60 s after last command |

Implements the Baseline Poll Cadence, Post-Command Burst Cadence, and
Cadence Decay Rule. The coordinator sets `update_interval` dynamically;
no separate scheduler is needed.

### 5. Client-side rate budget

A sliding-window `_RateLimiter` in `api.py` tracks requests over the last 10 s
and last 1 h. Polling consumes at most 80 % of each budget (Internal Budget Rule),
reserving headroom for user-triggered commands. The limiter raises
`NetatmoRateLimitError` before sending a request, so polling is skipped rather
than rejected by Netatmo (Rate-Aware Polling Rule, Throttling Rule).

```
RATE_LIMIT_PER_10S  = 50  →  polling budget: 40 req / 10 s
RATE_LIMIT_PER_HOUR = 500 →  polling budget: 400 req / hour
```

At 90 s baseline with one home: ≈ 40 req/hour. Burst (9 s × 60 s window) adds
at most 7 requests per command. Both are well within the polling budget.

### 6. Exponential backoff on errors

On 429 or 5xx, the coordinator doubles the backoff delay starting at 15 s (60 s
for 429), capping at 300 s (Backoff Rule). During backoff, stale data is returned
to entities so they remain available with stale semantics (Degradation Rule).
Consecutive error count resets to zero on the first successful poll.

### 7. Freshness model

| Age of last successful fetch | Entity state |
|---|---|
| < 3 min | fresh |
| 3 – 10 min | stale (available; `state_stale` attribute set) |
| > 10 min | unavailable |

Implements the Stale Threshold Rule and Unavailable Threshold Rule. Entities
always report confirmed provider state; they never show pending state as truth
(Consistency Priority, State Source of Truth).

### 8. At-most-once writes with pending window

After a command is sent:

1. `coordinator.trigger_burst()` registers a `PendingCommand` for the module.
2. Burst polling begins (see §4).
3. Each poll checks whether the returned state matches the pending command.
4. On match → command confirmed; pending cleared.
5. If no match after 90 s → `PENDING_TIMEOUT` expires; warning logged; pending
   cleared. Confirmed state remains authoritative (Unconfirmed Timeout Rule,
   Unknown Outcome Rule).

Commands are never blindly retried on uncertain outcome (Write Reliability Model).
A `command_pending` extra-state-attribute signals the window to the UI without
changing the reported HVAC mode or target temperature (Pending Update Rule).

### 9. Conservative mode advertisement

Each climate entity advertises `[HVACMode.OFF, HVACMode.COOL]` by default
(Conservative Mode Advertisement Rule). Additional modes are added only when
the runtime API confirms them for the specific device (Progressive Capability
Expansion Rule). Fan modes are discovered from the first `NacState` that
contains `fan_mode` or `fan_speed` data (Fan Capability Advertisement Rule).

### 10. Manual override duration

Cooling setpoint commands include an `endtime` calculated as
`now + override_duration_seconds`. Default is 3600 s (60 min), configurable
per config entry via the options flow (Default Override Duration, Duration
Configuration Rule). Individual service calls can supply a different duration
via a service call parameter (Per-Command Duration Override Rule).

### 11. OAuth2 with HA Application Credentials

Users register their Netatmo app client credentials in HA's Application
Credentials panel; the integration never stores account passwords
(Authentication Model, Credential UX Model, Token Storage Constraint).

Reauth flow:

```
Integration reports ConfigEntryAuthFailed
        │
        ▼
HA shows "Re-authorise" notification
        │
        ▼
User triggers reauth → async_step_reauth → async_step_user (OAuth2 again)
        │
        ▼
async_oauth_create_entry detects existing entry by unique_id
        │
        ▼
Token updated in-place; entry reloaded; home/module selection preserved
        │
        ▼
async_abort(reason="reauth_successful")
```

Satisfies the Reauthorization Expectation. Home and module selection is not
repeated on reauth (Reactivation Continuity Intent).

### 12. Onboarding and lifecycle

Setup follows the Select Homes → Select Devices flow with all entries
pre-checked (Onboarding Default Rule). Deselecting a device disables its
entity rather than removing it (Deselection Lifecycle Rule, Data Retention Intent).
Re-selecting reactivates the same entity identity (Reactivation Identity Rule).

---

## Out of scope (v1)

- Schedule CRUD (Netatmo Schedule Scope Rule)
- Neutral-schedule takeover (Deferred Scope Guardrail)
- Push / webhook updates (Push Feature Stance)
- Per-device custom control services beyond `force_refresh` (Custom Service Scope)

---

## Consequences

- Cloud dependency means the integration is unavailable during Netatmo outages,
  mitigated by the stale/unavailable degradation model.
- The control endpoint `/api/setcoolsetpoint` is inferred from API field naming
  conventions and must be validated against live Netatmo API documentation for
  NAC modules before a production release.
- Conservative mode advertisement means some AC features will initially appear
  absent; they expand automatically on first confirmed runtime data.
