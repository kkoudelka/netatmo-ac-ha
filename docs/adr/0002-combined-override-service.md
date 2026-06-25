# ADR-0002 — Combined Override Service with Per-Command Duration

**Status:** Accepted
**Date:** 2026-06-25
**Glossary:** All capitalised terms are defined in [`CONTEXT.md`](../../CONTEXT.md).
**Relation to ADR-0001:** Narrows the `Custom Service Scope (v1)` decision via the
`Combined Override Service Exception`. ADR-0001 remains accurate as the record of
the original v1 stance.

---

## Context

Automations turn the AC on by calling `climate.set_hvac_mode` and
`climate.set_temperature`, and can already call `climate.set_fan_mode`
(undocumented, but functional). Every one of these commands gets a manual
override duration from the per-config-entry default (`Default Override
Duration`), because Home Assistant's built-in climate services have fixed
schemas — there is no field on `climate.set_temperature` or
`climate.set_fan_mode` for a custom end time.

ADR-0001 deferred custom services "unless a proven capability gap exists"
(`Custom Service Scope (v1)`). Needing per-command control over override
duration — and the ability to set mode, temperature, and fan speed together
in one automation action — is that proven gap.

---

## Decision

### 1. New service: `netatmo_ac.set_state`

Fields, all optional:

| Field | Type | Notes |
|---|---|---|
| `hvac_mode` | `off` \| `cool` | Same vocabulary as `climate.set_hvac_mode` |
| `temperature` | float | Same range/step validation as `climate.set_temperature` |
| `fan_mode` | `low` \| `medium` \| `high` | Same vocabulary as `climate.set_fan_mode` |
| `duration` | int (minutes, 5–480) | Defaults to the entry's configured override duration |

Omitted fields mean "leave that aspect unchanged," matching existing partial-update
semantics on the standard climate services.

### 2. Shared duration

`duration` applies to whichever of cooling and fan are changed in the same
call (`Shared Override Duration Rule`). There is no independent
`duration_temperature` / `duration_fan`.

### 3. `off` cannot be combined with `temperature` or `fan_mode`

Raises `ServiceValidationError` (`Off-Combination Validation Rule`). Consistent
with the existing `No Silent Coercion Rule`: a parameter the caller explicitly
provided is never silently dropped.

### 4. Two sequential provider requests, not one combined POST

Cooling (`rooms`) and fan (`modules`) changes are sent as two separate
`/setstate` requests reusing the already-validated payload shapes from
`async_set_cool_setpoint` and `async_set_fan_speed`, issued together as one
user-facing command (`Sequential Write Composition Rule`). A single POST
combining both arrays was considered (would save one request against the
rate budget) but Netatmo's behavior for a combined payload is unverified, and
this repo's established practice is to validate endpoint behavior empirically
before relying on it. Revisit as a future optimization once validated live.

### 5. Fan commands get real pending-confirmation tracking

Pending-command tracking previously only checked target mode and target
temperature; fan-affecting commands trivially "confirmed" immediately because
no fan target was recorded. `Fan Confirmation Rule` closes this gap: pending
state now also tracks the requested fan state, so fan commands are confirmed
against provider state the same way mode/temperature commands already are.

### 6. Standard climate services are unchanged

`climate.set_temperature`, `climate.set_fan_mode`, and `climate.set_hvac_mode`
continue to use the per-entry default duration. `netatmo_ac.set_state` is
additive, not a replacement.

---

## Consequences

- A command that changes both temperature/mode and fan costs two provider
  requests instead of a hypothetical one; still well within the rate budget
  (see ADR-0001 §5).
- `netatmo_ac.set_state` is a public service surface the integration now
  commits to maintaining.
- The combined-single-POST optimization remains available as future work,
  gated on live validation against the Netatmo API.
