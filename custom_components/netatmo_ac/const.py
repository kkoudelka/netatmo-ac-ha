"""Constants for the Netatmo Smart AC Controller integration."""

DOMAIN = "netatmo_ac"

NETATMO_API_BASE = "https://api.netatmo.com/api"
NETATMO_SYNC_API_BASE = "https://app.netatmo.net/syncapi/v1"
NETATMO_AUTH_URL = "https://api.netatmo.com/oauth2/authorize"
NETATMO_TOKEN_URL = "https://api.netatmo.com/oauth2/token"

# Scopes required for AC control (read home topology + write setpoints)
OAUTH2_SCOPES = ["read_clim", "write_clim"]

# --- Polling cadence (CONTEXT: Baseline Poll Cadence, Post-Command Burst Cadence) ---
POLL_INTERVAL_BASELINE = 90       # seconds between polls at rest
POLL_INTERVAL_BURST = 9           # seconds during post-command burst (midpoint of 8-10 s)
BURST_DURATION = 60               # seconds to sustain burst cadence after a command

# --- Freshness (CONTEXT: Stale Threshold Rule, Unavailable Threshold Rule) ---
STALE_THRESHOLD = 180             # 3 min → mark entity state as stale
UNAVAILABLE_THRESHOLD = 600       # 10 min → mark entity unavailable

# --- Manual override duration (CONTEXT: Default Override Duration) ---
DEFAULT_OVERRIDE_DURATION_MINUTES = 60   # shown to user in minutes
DEFAULT_OVERRIDE_DURATION = DEFAULT_OVERRIDE_DURATION_MINUTES * 60  # stored in seconds

# --- Netatmo per-user rate limits (CONTEXT: Per-User Rate Constraint) ---
RATE_LIMIT_PER_10S = 50
RATE_LIMIT_PER_HOUR = 500
# Reserve 20 % headroom for user-triggered commands (CONTEXT: Internal Budget Rule)
RATE_POLLING_HEADROOM = 0.80

# --- Module type markers (CONTEXT: Confirmed AC Module Marker) ---
MODULE_TYPE_NAC = "NAC"

# --- Config entry data keys ---
CONF_HOME_IDS = "home_ids"
CONF_MODULE_IDS = "module_ids"
CONF_MODULE_NAMES = "module_names"             # {module_id: display_name} — stored at setup
CONF_OVERRIDE_DURATION = "override_duration"   # stored as seconds

# --- Config entry options keys ---
CONF_TEMP_SENSORS = "temp_sensors"             # {module_id: [entity_id, ...]} — in options

# --- Command pending timeout ---
PENDING_TIMEOUT = 90   # seconds; matches one baseline poll cycle
