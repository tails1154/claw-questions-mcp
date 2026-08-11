#!/usr/bin/env python3
"""
Claw Questions + Music — MCP server & relay.

Serves:
  * MCP (streamable-http) at /mcp — tools: ask_question, get_answer,
    play_music, pause_music, resume_music, stop_music, set_volume, music_status
  * Legacy Kodi addon endpoints:
      GET  /question  -> pending question JSON (or 204)
      POST /answer    -> receive answer from Kodi

Music plays through the Turtle Beach satellite speaker (mpg123 -> pulse).
"""

import asyncio
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import urllib.request

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.server.mcpserver import MCPServer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOST = "0.0.0.0"
PORT = 25568
MCP_PATH = "/mcp"

SND_DEVICE = "alsa_output.usb-Turtle_Beach_Turtle_Beach_Stream_Mic-01.analog-stereo"
MPG123 = "/usr/bin/mpg123"

# Home Assistant — for satellite TTS messaging
HA_BASE = os.environ.get("HA_BASE", "https://ha.tails1154.com")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# Satellite name -> HA entity id
SATELLITES = {
    "dad": "assist_satellite.dad_s_room_dad_room_satellite",
    "dadroom": "assist_satellite.dad_s_room_dad_room_satellite",
    "dad_room": "assist_satellite.dad_s_room_dad_room_satellite",
    "tails1154": "assist_satellite.computer_turtle_beach_satellite",
    "turtle": "assist_satellite.computer_turtle_beach_satellite",
    "turtlebeach": "assist_satellite.computer_turtle_beach_satellite",
    "computer": "assist_satellite.computer_turtle_beach_satellite",
}

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# Question relay state (Kodi addon <-> whoever asks via MCP)
_current_question: Optional[Dict[str, Any]] = None  # {id,type,question,options,answered,answer}

# Music playback state
_music: Dict[str, Any] = {"proc": None, "url": None, "paused": False}


# ---------------------------------------------------------------------------
# Music control
# ---------------------------------------------------------------------------

def _music_proc() -> Optional[subprocess.Popen]:
    proc = _music.get("proc")
    if proc is None:
        return None
    if proc.poll() is not None:
        # Process died; clear stale state
        _music["proc"] = None
        _music["url"] = None
        _music["paused"] = False
        return None
    return proc


def play_music(url: str) -> Dict[str, Any]:
    """Play an mp3 (or stream URL) through the satellite speaker."""
    url = url.strip()
    if not url:
        return {"ok": False, "error": "empty url"}

    stop_music()  # replace whatever is playing

    try:
        proc = subprocess.Popen(
            [MPG123, "-o", "pulse", "-a", SND_DEVICE, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}

    _music["proc"] = proc
    _music["url"] = url
    _music["paused"] = False
    return {"ok": True, "url": url, "pid": proc.pid, "paused": False}


def pause_music() -> Dict[str, Any]:
    """Pause music (SIGSTOP the decoder)."""
    proc = _music_proc()
    if proc is None:
        return {"ok": False, "error": "nothing playing"}
    try:
        proc.send_signal(signal.SIGSTOP)
    except ProcessLookupError:
        return {"ok": False, "error": "process gone"}
    _music["paused"] = True
    return {"ok": True, "paused": True, "url": _music["url"]}


def resume_music() -> Dict[str, Any]:
    """Resume paused music (SIGCONT the decoder)."""
    proc = _music_proc()
    if proc is None:
        return {"ok": False, "error": "nothing playing"}
    try:
        proc.send_signal(signal.SIGCONT)
    except ProcessLookupError:
        return {"ok": False, "error": "process gone"}
    _music["paused"] = False
    return {"ok": True, "paused": False, "url": _music["url"]}


def stop_music() -> Dict[str, Any]:
    """Stop music playback."""
    proc = _music.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
    _music["proc"] = None
    _music["url"] = None
    _music["paused"] = False
    return {"ok": True, "paused": False}


def set_volume(percent: int) -> Dict[str, Any]:
    """Set satellite speaker volume (0-100)."""
    percent = max(0, min(100, int(percent)))
    try:
        subprocess.run(
            ["pactl", "set-sink-volume", SND_DEVICE, f"{percent}%"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True, "volume": percent}


def music_status() -> Dict[str, Any]:
    """Current music playback status."""
    proc = _music_proc()
    playing = proc is not None
    return {
        "playing": playing,
        "paused": _music["paused"] if playing else False,
        "url": _music["url"] if playing else None,
        "pid": proc.pid if playing else None,
    }


# ---------------------------------------------------------------------------
# Question relay
# ---------------------------------------------------------------------------

def ask_question(question: str, options: Optional[List[str]] = None, qtype: str = "select") -> Dict[str, Any]:
    """Ask a question on the TV (Kodi dialog). Returns the question id."""
    global _current_question
    if qtype not in ("select", "input"):
        qtype = "select"
    qid = uuid.uuid4().hex[:8]
    _current_question = {
        "id": qid,
        "type": qtype,
        "question": question,
        "options": options if options else (["Yes", "No"] if qtype == "select" else []),
        "answered": False,
        "answer": None,
    }
    return {"ok": True, "id": qid, "question": question, "type": qtype}


def get_answer(question_id: Optional[str] = None) -> Dict[str, Any]:
    """Get the answer to a question (poll after asking)."""
    global _current_question
    q = _current_question
    if q is None:
        return {"ok": False, "error": "no question pending"}
    if question_id is not None and q["id"] != question_id:
        return {"ok": False, "error": "question id mismatch"}
    if not q["answered"]:
        return {"ok": True, "id": q["id"], "answered": False, "answer": None}
    return {"ok": True, "id": q["id"], "answered": True, "answer": q["answer"]}


# ---------------------------------------------------------------------------
# Satellite TTS messaging (via Home Assistant assist_satellite.announce)
# ---------------------------------------------------------------------------

def _ha_call(service: str, payload: dict) -> Dict[str, Any]:
    if not HA_TOKEN:
        return {"ok": False, "error": "HA_TOKEN not set"}
    url = f"{HA_BASE.rstrip('/')}/api/services/{service}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode()
            return {"ok": True, "status": resp.status, "body": body[:2000]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def satellite_announce(satellite: str, message: str) -> Dict[str, Any]:
    """Play a TTS message aloud on a satellite. satellite: 'dad' or 'turtle'."""
    entity = SATELLITES.get(satellite.strip().lower())
    if entity is None:
        return {"ok": False, "error": f"unknown satellite '{satellite}' (try: dad, turtle)"}
    if not message or not message.strip():
        return {"ok": False, "error": "empty message"}
    result = _ha_call(
        "assist_satellite/announce",
        {"entity_id": entity, "message": message.strip(), "preannounce": False},
    )
    if not result.get("ok"):
        return result
    return {"ok": True, "satellite": satellite, "entity": entity, "message": message.strip()}


def satellite_list() -> Dict[str, Any]:
    """List available satellites and their entity ids."""
    return {"ok": True, "satellites": [
        {"name": k, "entity": v} for k, v in SATELLITES.items() if k in ("dad", "tails1154")
    ]}


# ---------------------------------------------------------------------------
# MQTT (rings the Pi alarm clock buzzer + HA command bridge)
# ---------------------------------------------------------------------------
#
# Topics (HA -> server, subscribed):
#   claw/timer/set      {"seconds": 300, "label": "...", "satellite": "..."}  or plain seconds
#   claw/alarm/set      {"clock_time": "07:30", "label": "...", "satellite": "..."}  or plain "07:30"
#   claw/timer/cancel   {"id": "t1"}  or plain "t1"
#   claw/timer/modify   {"id": "t1", "seconds": 600, ...}
#   claw/dismiss        anything
#
# Topics (server -> HA, published):
#   claw/timers/state   retained JSON {"count": N, "timers": [...]}
#   claw/status         retained "online" / LWT "offline"
#
# MQTT Discovery configs are published under homeassistant/ so HA auto-creates
# entities (sensor, text x3, button, binary_sensor) — no custom integration.

MQTT_BROKER = "192.168.0.149"
MQTT_PORT = 1883
MQTT_USER = "serverstatus"
MQTT_PASS = "serverstatus"

TOPIC_TIMER_SET = "claw/timer/set"
TOPIC_ALARM_SET = "claw/alarm/set"
TOPIC_TIMER_CANCEL = "claw/timer/cancel"
TOPIC_TIMER_MODIFY = "claw/timer/modify"
TOPIC_TIMER_CLEAR = "claw/timer/clear"
TOPIC_ANNOUNCE = "claw/announce"
TOPIC_DISMISS = "claw/dismiss"
TOPIC_TIMER_STATE = "claw/timers/state"
TOPIC_STATUS = "claw/status"

SUB_TOPICS = [
    TOPIC_TIMER_SET,
    TOPIC_ALARM_SET,
    TOPIC_TIMER_CANCEL,
    TOPIC_TIMER_MODIFY,
    TOPIC_TIMER_CLEAR,
    TOPIC_ANNOUNCE,
    TOPIC_DISMISS,
]

_mqtt_client = None
_mqtt_lock = threading.Lock()


def _mqtt_ensure() -> bool:
    """Create (once) and return the persistent MQTT client. Safe to call from any thread."""
    global _mqtt_client
    with _mqtt_lock:
        if _mqtt_client is not None:
            return True
        try:
            import paho.mqtt.client as mqtt  # noqa: PLC0415
            c = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id="claw_questions",
                protocol=mqtt.MQTTv311,
            )
            c.username_pw_set(MQTT_USER, MQTT_PASS)
            c.will_set(TOPIC_STATUS, "offline", qos=1, retain=True)
            c.on_connect = _mqtt_on_connect
            c.on_message = _mqtt_on_message
            c.connect(MQTT_BROKER, MQTT_PORT, 60)
            c.loop_start()
            _mqtt_client = c
            return True
        except Exception as e:  # noqa: BLE001
            print(f"MQTT init failed: {e}", flush=True)
            return False


def _mqtt_on_connect(client, userdata, flags, reason_code, properties=None):
    if getattr(reason_code, "is_failure", False):
        print(f"MQTT connect failed: {reason_code}", flush=True)
        return
    print("MQTT connected", flush=True)
    for topic in SUB_TOPICS:
        client.subscribe(topic, qos=1)
    # Do NOT block the network loop here (no wait_for_publish): spawn a thread.
    client.publish(TOPIC_STATUS, "online", qos=1, retain=True)
    threading.Thread(target=_mqtt_on_connect_post, daemon=True).start()


def _mqtt_on_connect_post() -> None:
    """Run after connect on a separate thread: discovery + timer state (blocking OK)."""
    _mqtt_publish_discovery()
    _mqtt_publish_timer_state()


def _mqtt_pub(topic: str, payload: str, retain: bool = False) -> bool:
    if not _mqtt_ensure():
        return False
    try:
        info = _mqtt_client.publish(topic, payload, qos=1, retain=retain)
        info.wait_for_publish(timeout=5)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"MQTT publish failed: {e}", flush=True)
        return False


# --- timer state + discovery -------------------------------------------------

_DEVICE = {
    "identifiers": ["claw_questions_mcp"],
    "name": "Claw Questions",
    "manufacturer": "tails1154",
    "model": "claw-questions-mcp",
    "sw_version": "1.1.0",
}


def _mqtt_discovery(component: str, obj_id: str, name: str, **extra) -> str:
    payload = {
        "name": name,
        "unique_id": f"claw_{obj_id}",
        "device": _DEVICE,
        **extra,
    }
    return json.dumps(payload)


def _mqtt_publish_discovery() -> None:
    """Publish retained MQTT discovery configs so HA auto-creates entities."""
    try:
        _mqtt_pub(
            "homeassistant/sensor/claw_timers/config",
            _mqtt_discovery(
                "sensor", "active_timers", "Claw Active Timers",
                state_topic=TOPIC_TIMER_STATE,
                value_template="{{ value_json.count }}",
                json_attributes_topic=TOPIC_TIMER_STATE,
                json_attributes_template='{"timers": {{ value_json.timers | tojson }}}',
                unit_of_measurement="timers",
                icon="mdi:timer-outline",
            ),
            retain=True,
        )
        _mqtt_pub(
            "homeassistant/text/claw_timer_set/config",
            _mqtt_discovery(
                "text", "set_timer", "Claw Set Timer (seconds)",
                command_topic=TOPIC_TIMER_SET,
                mode="text",
                icon="mdi:timer-plus-outline",
            ),
            retain=True,
        )
        _mqtt_pub(
            "homeassistant/text/claw_alarm_set/config",
            _mqtt_discovery(
                "text", "set_alarm", "Claw Set Alarm (HH:MM)",
                command_topic=TOPIC_ALARM_SET,
                mode="text",
                icon="mdi:alarm",
            ),
            retain=True,
        )
        _mqtt_pub(
            "homeassistant/text/claw_timer_cancel/config",
            _mqtt_discovery(
                "text", "cancel_timer", "Claw Cancel Timer (id)",
                command_topic=TOPIC_TIMER_CANCEL,
                mode="text",
                icon="mdi:timer-off-outline",
            ),
            retain=True,
        )
        _mqtt_pub(
            "homeassistant/button/claw_dismiss/config",
            _mqtt_discovery(
                "button", "dismiss", "Claw Dismiss Alarm",
                command_topic=TOPIC_DISMISS,
                payload_press="dismiss",
                icon="mdi:alarm-off",
            ),
            retain=True,
        )
        _mqtt_pub(
            "homeassistant/binary_sensor/claw_bridge/config",
            _mqtt_discovery(
                "binary_sensor", "bridge_online", "Claw Bridge Online",
                state_topic=TOPIC_STATUS,
                payload_on="online",
                payload_off="offline",
                device_class="connectivity",
            ),
            retain=True,
        )
        print("MQTT discovery published", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"MQTT discovery failed: {e}", flush=True)


def _mqtt_publish_timer_state() -> None:
    now = time.time()
    timers = []
    with _timer_lock:
        for tid, t in list(_timers.items()):
            timers.append({
                "id": tid,
                "kind": t.get("kind"),
                "label": t.get("label"),
                "remaining": max(0, t["due"] - now),
                "satellite": t.get("satellite"),
            })
    payload = json.dumps({"count": len(timers), "timers": timers})
    _mqtt_pub(TOPIC_TIMER_STATE, payload, retain=True)


# --- command handlers ---------------------------------------------------------

def _payload_text(payload) -> str:
    if isinstance(payload, bytes):
        return payload.decode(errors="replace").strip()
    return str(payload).strip()


def _mqtt_on_message(client, userdata, msg):
    # paho calls this on its network loop thread — NEVER block it with
    # wait_for_publish. Hand off to a worker thread instead.
    _mqtt_command_queue.put((msg.topic, _payload_text(msg.payload)))


def _mqtt_command_worker():
    """Process MQTT commands on a dedicated thread (blocking publishes OK here)."""
    while True:
        topic, payload = _mqtt_command_queue.get()
        try:
            if topic == TOPIC_TIMER_SET:
                data = None
                try:
                    data = json.loads(payload)
                except Exception:  # noqa: BLE001
                    pass
                if isinstance(data, dict):
                    seconds = float(data.get("seconds", 0))
                    label = str(data.get("label", "timer"))
                    satellite = str(data.get("satellite", "tails1154"))
                else:
                    seconds = float(payload)
                    label, satellite = "timer", "tails1154"
                print(f"MQTT set_timer {seconds}s '{label}' -> {satellite}", flush=True)
                set_timer(seconds, label, satellite)
            elif topic == TOPIC_ALARM_SET:
                data = None
                try:
                    data = json.loads(payload)
                except Exception:  # noqa: BLE001
                    pass
                if isinstance(data, dict):
                    clock_time = str(data.get("clock_time", ""))
                    label = str(data.get("label", "alarm"))
                    satellite = str(data.get("satellite", "tails1154"))
                else:
                    clock_time, label, satellite = payload, "alarm", "tails1154"
                print(f"MQTT set_alarm {clock_time} '{label}' -> {satellite}", flush=True)
                set_alarm(clock_time, label, satellite)
            elif topic == TOPIC_TIMER_CANCEL:
                data = None
                try:
                    data = json.loads(payload)
                except Exception:  # noqa: BLE001
                    pass
                tid = data.get("id") if isinstance(data, dict) else payload
                print(f"MQTT cancel_timer {tid}", flush=True)
                cancel_timer(str(tid))
            elif topic == TOPIC_TIMER_MODIFY:
                data = json.loads(payload)
                print(f"MQTT modify_timer {data.get('id')}", flush=True)
                modify_timer(
                    str(data.get("id", "")),
                    seconds=data.get("seconds"),
                    label=data.get("label"),
                    satellite=data.get("satellite"),
                )
            elif topic == TOPIC_TIMER_CLEAR:
                print("MQTT clear_timers", flush=True)
                clear_timers()
            elif topic == TOPIC_ANNOUNCE:
                data = None
                try:
                    data = json.loads(payload)
                except Exception:  # noqa: BLE001
                    pass
                if isinstance(data, dict):
                    satellite = str(data.get("satellite", "tails1154"))
                    message = str(data.get("message", ""))
                else:
                    satellite, message = "tails1154", payload
                print(f"MQTT announce [{satellite}]: {message}", flush=True)
                satellite_announce(satellite, message)
            elif topic == TOPIC_DISMISS:
                print("MQTT dismiss", flush=True)
                _mqtt_pub("alarm/dismiss", "dismiss")
            _mqtt_publish_timer_state()
        except Exception as e:  # noqa: BLE001
            print(f"MQTT handler error on {topic}: {e}", flush=True)


# ---------------------------------------------------------------------------
# Alarms & timers (run in-process; fire a TTS announce + optional music)
# ---------------------------------------------------------------------------

_timers: Dict[str, Dict[str, Any]] = {}
_timer_seq = 0
_timer_lock = threading.Lock()


def _fire_timer(tid: str, t: Optional[Dict[str, Any]] = None) -> None:
    if t is None:
        with _timer_lock:
            t = _timers.get(tid)
    if not t:
        return
    label = t.get("label") or "timer"
    print(f"TIMER FIRED: {label}", flush=True)
    # Ring the Pi alarm clock buzzer (via MQTT) — alarms go through the physical
    # alarm clock, not a satellite TTS announce. Rings until dismissed
    # (physical button on the Pi, or POST /dismiss / MQTT claw/dismiss).
    _mqtt_pub("alarm/ring", "ring")


def _timer_worker() -> None:
    while True:
        now = time.time()
        fired = []
        with _timer_lock:
            for tid, t in list(_timers.items()):
                if not t.get("paused") and now >= t.get("due", 0):
                    fired.append((tid, t))
            for tid, t in fired:
                t = _timers.pop(tid, None)
                if t and t.get("repeat", 0) > 0:
                    t["due"] = now + t["repeat"]
                    _timers[tid] = t
        if fired:
            _mqtt_publish_timer_state()
        for tid, t in fired:
            _fire_timer(tid, t)
        time.sleep(1)


threading.Thread(target=_timer_worker, daemon=True).start()


def set_timer(seconds: float, label: str = "timer", satellite: str = "tails1154") -> Dict[str, Any]:
    """Set a countdown timer (seconds). Fires TTS on the satellite when done."""
    global _timer_seq
    seconds = float(seconds)
    with _timer_lock:
        _timer_seq += 1
        tid = f"t{_timer_seq}"
        _timers[tid] = {
            "kind": "timer",
            "due": time.time() + seconds,
            "label": label,
            "satellite": satellite,
            "paused": False,
            "repeat": 0,
            "created": time.time(),
        }
    _mqtt_publish_timer_state()
    return {"ok": True, "id": tid, "kind": "timer", "due_in": seconds, "label": label}


def set_alarm(clock_time: str, label: str = "alarm", satellite: str = "tails1154") -> Dict[str, Any]:
    """Set an alarm at a wall-clock time like '07:30'. Fires TTS on the satellite."""
    global _timer_seq
    try:
        import datetime as _dt
        hh, mm = clock_time.strip().split(":")
        hh, mm = int(hh), int(mm)
        now = _dt.datetime.now()
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if due <= now:
            due += _dt.timedelta(days=1)  # tomorrow if already past
        seconds = (due - now).total_seconds()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"bad time '{clock_time}': {e}"}
    with _timer_lock:
        _timer_seq += 1
        tid = f"t{_timer_seq}"
        _timers[tid] = {
            "kind": "alarm",
            "due": time.time() + seconds,
            "label": label,
            "satellite": satellite,
            "paused": False,
            "repeat": 0,
            "created": time.time(),
        }
    _mqtt_publish_timer_state()
    return {"ok": True, "id": tid, "kind": "alarm", "due_in": seconds, "at": clock_time, "label": label}


def cancel_timer(timer_id: str) -> Dict[str, Any]:
    """Cancel an alarm/timer by id."""
    with _timer_lock:
        t = _timers.pop(timer_id, None)
    if t is None:
        return {"ok": False, "error": f"no timer with id {timer_id}"}
    _mqtt_publish_timer_state()
    return {"ok": True, "cancelled": timer_id, "label": t.get("label")}


def modify_timer(timer_id: str, seconds: Optional[float] = None, label: Optional[str] = None,
                 satellite: Optional[str] = None) -> Dict[str, Any]:
    """Modify a timer: change countdown (seconds), label, or satellite."""
    with _timer_lock:
        t = _timers.get(timer_id)
        if t is None:
            return {"ok": False, "error": f"no timer with id {timer_id}"}
        if seconds is not None:
            t["due"] = time.time() + float(seconds)
        if label is not None:
            t["label"] = label
        if satellite is not None:
            t["satellite"] = satellite
    _mqtt_publish_timer_state()
    return {"ok": True, "id": timer_id, "label": t.get("label"), "due_in": max(0, t["due"] - time.time())}


def list_timers() -> Dict[str, Any]:
    """List active alarms/timers."""
    now = time.time()
    out = []
    with _timer_lock:
        for tid, t in list(_timers.items()):
            out.append({
                "id": tid, "kind": t.get("kind"), "label": t.get("label"),
                "remaining": max(0, t["due"] - now),
                "satellite": t.get("satellite"),
            })
    return {"ok": True, "timers": out}


def clear_timers() -> Dict[str, Any]:
    """Cancel ALL alarms and timers at once."""
    with _timer_lock:
        count = len(_timers)
        _timers.clear()
    _mqtt_publish_timer_state()
    return {"ok": True, "cleared": count}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = MCPServer(
    name="claw-questions",
    title="Claw Questions & Music",
    description="Ask questions on the Kodi TV and play/control music on the satellite speaker.",
    version="1.0.0",
)

server.add_tool(play_music, name="play_music", description=play_music.__doc__)
server.add_tool(pause_music, name="pause_music", description=pause_music.__doc__)
server.add_tool(resume_music, name="resume_music", description=resume_music.__doc__)
server.add_tool(stop_music, name="stop_music", description=stop_music.__doc__)
server.add_tool(set_volume, name="set_volume", description=set_volume.__doc__)
server.add_tool(music_status, name="music_status", description=music_status.__doc__)
server.add_tool(ask_question, name="ask_question", description=ask_question.__doc__)
server.add_tool(get_answer, name="get_answer", description=get_answer.__doc__)
server.add_tool(satellite_announce, name="satellite_announce", description=satellite_announce.__doc__)
server.add_tool(satellite_list, name="satellite_list", description=satellite_list.__doc__)
server.add_tool(set_timer, name="set_timer", description=set_timer.__doc__)
server.add_tool(set_alarm, name="set_alarm", description=set_alarm.__doc__)
server.add_tool(cancel_timer, name="cancel_timer", description=cancel_timer.__doc__)
server.add_tool(modify_timer, name="modify_timer", description=modify_timer.__doc__)
server.add_tool(list_timers, name="list_timers", description=list_timers.__doc__)
server.add_tool(clear_timers, name="clear_timers", description=clear_timers.__doc__)


# ---------------------------------------------------------------------------
# HTTP app: MCP + legacy Kodi endpoints
# ---------------------------------------------------------------------------

app: Starlette = server.streamable_http_app(streamable_http_path=MCP_PATH)


async def get_question(request: Request) -> Response:
    """Legacy: Kodi addon polls this. Returns pending question or 204."""
    q = _current_question
    if q is None or q["answered"]:
        return Response(status_code=204)
    return JSONResponse(q)


app.add_route("/question", get_question, methods=["GET"])


async def post_answer(request: Request) -> Response:
    """Legacy: Kodi addon posts an answer here."""
    global _current_question
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    qid = body.get("id")
    answer = body.get("answer")
    if _current_question is None or _current_question["id"] != qid:
        return JSONResponse({"ok": False, "error": "unknown question id"}, status_code=404)

    _current_question["answered"] = True
    _current_question["answer"] = answer
    return JSONResponse({"ok": True, "id": qid, "answer": answer})


app.add_route("/answer", post_answer, methods=["POST"])


async def status(request: Request) -> Response:
    return JSONResponse({"service": "claw-questions", "mcp": MCP_PATH, "music": music_status()})


app.add_route("/status", status, methods=["GET"])


# ---------------------------------------------------------------------------
# HTTP API: alarms & timers (plain JSON, no MCP needed)
# ---------------------------------------------------------------------------

async def http_set_timer(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    try:
        seconds = float(body.get("seconds", 0))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "seconds must be a number"}, status_code=400)
    result = set_timer(
        seconds=seconds,
        label=str(body.get("label", "timer")),
        satellite=str(body.get("satellite", "tails1154")),
    )
    code = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=code)


async def http_set_alarm(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    result = set_alarm(
        clock_time=str(body.get("clock_time", "")),
        label=str(body.get("label", "alarm")),
        satellite=str(body.get("satellite", "tails1154")),
    )
    code = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=code)


async def http_list_timers(request: Request) -> Response:
    return JSONResponse(list_timers())


async def http_clear_timers(request: Request) -> Response:
    return JSONResponse(clear_timers())


async def http_cancel_timer(request: Request) -> Response:
    timer_id = request.path_params.get("timer_id", "")
    result = cancel_timer(timer_id)
    code = 200 if result.get("ok") else 404
    return JSONResponse(result, status_code=code)


async def http_modify_timer(request: Request) -> Response:
    timer_id = request.path_params.get("timer_id", "")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    result = modify_timer(
        timer_id,
        seconds=body.get("seconds"),
        label=body.get("label"),
        satellite=body.get("satellite"),
    )
    code = 200 if result.get("ok") else 404
    return JSONResponse(result, status_code=code)


async def http_dismiss(request: Request) -> Response:
    _mqtt_pub("alarm/dismiss", "dismiss")
    return JSONResponse({"ok": True, "dismissed": True})


app.add_route("/timer", http_set_timer, methods=["POST"])
app.add_route("/alarm", http_set_alarm, methods=["POST"])
app.add_route("/timers", http_list_timers, methods=["GET"])
app.add_route("/timers", http_clear_timers, methods=["DELETE"])
app.add_route("/timer/{timer_id}", http_cancel_timer, methods=["DELETE"])
app.add_route("/timer/{timer_id}", http_modify_timer, methods=["PATCH"])
app.add_route("/dismiss", http_dismiss, methods=["POST"])


_mqtt_command_queue: "queue.Queue" = queue.Queue()


def main() -> None:
    _mqtt_ensure()  # connect to MQTT broker + publish HA discovery + subscribe commands
    threading.Thread(target=_mqtt_command_worker, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
