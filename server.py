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
import re
import signal
import subprocess
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
        {"name": k, "entity": v} for k, v in SATELLITES.items() if k in ("dad", "turtle")
    ]}


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


def main() -> None:
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
