# Netatmo Smart AC Controller — Home Assistant Integration

Unofficial Home Assistant custom integration for the **Netatmo Smart AC Controller (NAC)**. Exposes the AC unit as a `climate` entity with full on/off, temperature setpoint, and fan speed control.

---

## Features

- **On/Off and cooling setpoint** — set target temperature and toggle cooling mode
- **Fan speed** — low / medium / high
- **Adaptive polling** — 90 s baseline, bursts to 9 s for 60 s after each command
- **Temperature sensor override** — use any HA sensor (or average multiple) instead of the NAC's built-in sensor
- **Reauth flow** — re-authorise without losing your device selection
- **Diagnostics** — redacted snapshot available under the device page

---

## Prerequisites

- Home Assistant 2024.1 or newer
- A [Netatmo account](https://www.netatmo.com) with the Smart AC Controller paired in the **Netatmo Control** app
- A Netatmo developer application (see below)

---

## 1 — Create a Netatmo developer application

1. Go to [dev.netatmo.com/apps](https://dev.netatmo.com/apps) and log in with your Netatmo account.
2. Click **Create an app**.
3. Fill in a name (e.g. _Home Assistant AC_). The other fields can be left blank.
4. Under **Redirect URI**, add:
   ```
   https://<your-home-assistant-url>/auth/external/callback
   ```
   Replace `<your-home-assistant-url>` with the external URL of your HA instance (e.g. `https://homeassistant.local:8123`). It must be reachable from your browser during the OAuth2 flow.
5. Save the app and note down the **Client ID** and **Client Secret**.

---

## 2 — Install the integration

### Manual (recommended until HACS support is added)

1. Copy the `custom_components/netatmo_ac` folder into your HA `config` directory so the path is:
   ```
   config/custom_components/netatmo_ac/
   ```
2. Restart Home Assistant.

### HACS (not yet listed)

Once this repository is added to HACS as a custom repository:

1. **HACS → Integrations → ⋮ → Custom repositories**
2. Add this repository URL, category **Integration**
3. Install _Netatmo Smart AC Controller_ and restart HA

---

## 3 — Add Application Credentials

Before adding the integration, register your OAuth2 credentials with HA:

1. Go to **Settings → Devices & Services → Application Credentials** (or navigate to `/config/application_credentials`).
2. Click **Add credentials**.
3. Select **Netatmo Smart AC Controller** from the integration list.
4. Enter any name, then paste your **Client ID** and **Client Secret** from step 1.
5. Click **Add**.

---

## 4 — Set up the integration

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **Netatmo Smart AC Controller**.
3. Click through the OAuth2 authorisation — your browser will open the Netatmo login page.
   - Make sure to grant all requested permissions (`read_clim`, `write_clim`).
4. **Select homes** — all detected Netatmo homes are pre-checked. Deselect any you don't want.
5. **Select AC units** — all NAC modules found are pre-checked.
6. **Override duration** — how long a manual setpoint change lasts before Netatmo's schedule resumes (default: 60 minutes).
7. Click **Submit**. The integration creates one `climate` entity per AC unit.

---

## 5 — Configure options (after setup)

Go to **Settings → Devices & Services → Netatmo Smart AC Controller → Configure** to change:

| Option             | Description                                                                                                                                       |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Override duration  | How long manual setpoint changes last (5–480 min)                                                                                                 |
| Temperature source | One or more HA temperature sensors to use instead of the NAC's built-in sensor. Multiple sensors are averaged. Leave empty to use the NAC sensor. |

### Why override the temperature sensor?

The NAC module sits next to the AC unit and can read artificially low temperatures due to the cold air outlet. Selecting a sensor placed elsewhere in the room gives a more accurate reading for automations and the HA thermostat card.

---

## Automation example

Turns the AC on at 09:30 and off at 21:00.

```yaml
alias: AC — time schedule
triggers:
  - trigger: time
    at: "09:30:00"
    id: time_start
  - trigger: time
    at: "21:00:00"
    id: time_end

actions:
  - choose:
      - conditions:
          - condition: trigger
            id: time_start
        sequence:
          - action: climate.set_hvac_mode
            target:
              entity_id: climate.ac_delonghi
            data:
              hvac_mode: cool
          - action: climate.set_temperature
            target:
              entity_id: climate.ac_delonghi
            data:
              temperature: 22
      - conditions:
          - condition: trigger
            id: time_end
        sequence:
          - action: climate.set_hvac_mode
            target:
              entity_id: climate.ac_delonghi
            data:
              hvac_mode: "off"
```

---

## Troubleshooting

### "Config flow could not be loaded"

The most common cause is a missing or malformed file after a manual update. Check **Settings → System → Logs** for a Python import error mentioning `netatmo_ac`. The log entry will name the exact file and line.

### "No Smart AC Controller devices found"

- Confirm the AC is paired and visible in the **Netatmo Control** app (not the Energy or Weather app).
- During OAuth2, ensure you grant both `read_clim` and `write_clim` scopes. If the consent screen did not show these, delete the Application Credentials entry, recreate it, and add the integration again.

### Entity is unavailable

The NAC module has not been reachable for more than 10 minutes. Check that the module has power and a Wi-Fi connection. The integration will mark it available again automatically once it responds.

### Commands have no effect

Commands go through `app.netatmo.net/syncapi/v1/setstate`. If the Netatmo cloud is reachable but commands are ignored, try controlling the unit from the Netatmo app to confirm it responds, then check HA logs for any `API error` messages.

---

## Technical notes

| Detail              | Value                                                      |
| ------------------- | ---------------------------------------------------------- |
| Polling baseline    | 90 s                                                       |
| Post-command burst  | 9 s for 60 s                                               |
| Rate limit headroom | 80 % of Netatmo's 500 req/hour budget reserved for polling |
| Write endpoint      | `https://app.netatmo.net/syncapi/v1/setstate`              |
| Read endpoints      | `https://api.netatmo.com/api/homesdata`, `/homestatus`     |
| OAuth2 scopes       | `read_clim write_clim`                                     |

---

## License

MIT
