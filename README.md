# claw-questions-mcp

MCP server for Claw's voice/assistant features:

- **Ask questions** on the Kodi TV (dialogs)
- **Play/pause/stop music** on the Turtle Beach satellite speaker
- **Alarms & timers** — countdown timers and wall-clock alarms that ring the Pi alarm clock buzzer + announce TTS on satellites
- **Satellite TTS** — announce messages on any satellite

Serves an MCP endpoint (`/mcp`, streamable-http), a legacy HTTP JSON API for the Kodi addon, and an **MQTT bridge** so Home Assistant can control everything natively.

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # or: mcp uvicorn paho-mqtt
cp ha.env.example ha.env                    # HA_TOKEN required for TTS/announce
.venv/bin/python server.py                  # listens on 0.0.0.0:25568
```

Systemd user unit (example):

```ini
[Unit]
Description=Claw Questions & Music MCP Server
After=network-online.target pipewire.service pipewire-pulse.service

[Service]
Type=simple
EnvironmentFile=/path/to/ha.env
ExecStart=/path/to/.venv/bin/python /path/to/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

## MCP tools

| Tool | Purpose |
|---|---|
| `ask_question` / `get_answer` | Kodi TV dialog questions |
| `play_music` / `pause_music` / `resume_music` / `stop_music` | Satellite speaker |
| `set_volume` / `music_status` | Volume + status |
| `satellite_announce` / `satellite_list` | TTS on satellites |
| `set_timer` / `set_alarm` / `cancel_timer` / `modify_timer` / `list_timers` | Alarms & timers |

## HTTP API (127.0.0.1:25568)

| Method | Path | Body | Purpose |
|---|---|---|---|
| POST | `/timer` | `{"seconds": 300, "label": "pizza", "satellite": "tails1154"}` | Countdown timer |
| POST | `/alarm` | `{"clock_time": "07:30", "label": "wake up", "satellite": "dad"}` | Wall-clock alarm |
| GET | `/timers` | — | List timers |
| PATCH | `/timer/{id}` | `{"seconds": 600}` / `{"label": ...}` | Modify |
| DELETE | `/timer/{id}` | — | Cancel |
| POST | `/dismiss` | — | Stop the buzzer |
| GET | `/status` | — | Service status |

## MQTT bridge (Home Assistant)

The server connects to the MQTT broker and exposes **command topics** (subscribed)
plus **status topics** (published, retained). It also publishes **MQTT Discovery**
configs under `homeassistant/` so HA auto-creates entities — **no custom integration
needed**; works with any MQTT setup (Mosquitto add-on etc.).

### Command topics (HA → server)

| Topic | Payload | Purpose |
|---|---|---|
| `claw/timer/set` | `{"seconds": 300, "label": "...", "satellite": "..."}` or plain `300` | Set countdown timer (seconds) |
| `claw/alarm/set` | `{"clock_time": "07:30", "label": "...", "satellite": "..."}` or plain `07:30` | Set wall-clock alarm |
| `claw/timer/cancel` | `{"id": "t1"}` or plain `t1` | Cancel timer/alarm by id |
| `claw/timer/modify` | `{"id": "t1", "seconds": 600, "label": "..."}` | Modify timer |
| `claw/dismiss` | anything | Stop the Pi buzzer |

### Status topics (server → HA)

| Topic | Payload | Purpose |
|---|---|---|
| `claw/timers/state` | `{"count": N, "timers": [...]}` (retained) | Active timers |
| `claw/status` | `online` / `offline` (retained + LWT) | Bridge liveness |

### Auto-created HA entities (MQTT discovery)

- `sensor.claw_questions_claw_active_timers` — active timer count
- `text.claw_questions_claw_set_timer_seconds` — type seconds, e.g. `600`
- `text.claw_questions_claw_set_alarm_hh_mm` — type time, e.g. `07:30`
- `text.claw_questions_claw_cancel_timer_id` — type timer id, e.g. `t1`
- `button.claw_questions_claw_dismiss_alarm` — stop the buzzer
- `binary_sensor.claw_questions_claw_bridge_online` — bridge alive

Example HA automation (timer via MQTT publish):

```yaml
action: mqtt.publish
data:
  topic: claw/timer/set
  payload: '{"seconds": 600, "label": "pizza", "satellite": "tails1154"}'
```

### Timer firing behavior

When a timer/alarm fires:

1. Publishes MQTT `alarm/ring` → the Pi alarm clock buzzer rings until dismissed
2. Announces TTS on the chosen satellite via HA `assist_satellite.announce`
   ("Timer. {label}")

### Satellite names

- `tails1154` → Turtle Beach satellite (default)
- `dad` → Dad Room satellite (Pi 3B)

## Notes

- Timer ids are short strings (`t1`, `t2`) — re-fetch `/timers` or read `claw/timers/state`
  to get the current id before modifying/cancelling.
- Timers live in process memory — a service restart clears them.
- `clock_time` is strict 24h `HH:MM`; past times roll to tomorrow.
- The MQTT client uses paho-mqtt v2 (`CallbackAPIVersion.VERSION2`); keepalive 60s.
- MQTT creds default to `serverstatus`/`serverstatus` on `192.168.0.149:1883` — edit
  `MQTT_BROKER` / `MQTT_USER` / `MQTT_PASS` at the top of `server.py` for other setups.
