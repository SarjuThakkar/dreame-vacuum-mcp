"""Voice control for a Dreame robot vacuum over Matter, for the Pebble Index ring.

Pebble's cloud agent talks MCP to this server; this server talks WebSocket to a
local Matter controller (python-matter-server), which talks Matter to the
vacuum over the LAN. Everything except Pebble's agent runs on the same Pi.

    export MCP_BEARER_TOKEN=$(openssl rand -hex 32)
    export MATTER_URL=ws://host.docker.internal:5580/ws
    export MATTER_NODE_ID=1

Why Matter and not Dreame's cloud API: the Matrix 10 isn't in the community
`dreame-vacuum` integration's supported list, and Dreame publishes no official
HTTP API. Matter is vendor-sanctioned, entirely local (no cloud round-trip,
works if the internet is down), and the device exposes exactly the clusters
this needs -- including ServiceArea, which carries the real room map the user
already drew in the Dreame app.

Clusters used, all on endpoint 1 (verified against the real device, firmware
4.3.9_3835 -- the accepted-command lists below are what it actually reports,
not what the spec permits):

    84  RvcRunMode           ChangeToMode  -- start (Cleaning) / stop (Idle)
    85  RvcCleanMode         ChangeToMode  -- vacuum vs mop
    97  RvcOperationalState  Pause, Resume, GoHome
    336 ServiceArea          SelectAreas   -- room targeting

Device quirk worth knowing: sending GoHome while a clean is running is
ACKed with no error but does not actually end the job. What ends it is
RvcRunMode -> Idle, after which the vacuum goes to SeekingCharger on its
own. So `dock` sets Idle first and only then sends GoHome. A second quirk:
RvcRunMode keeps reporting Cleaning even once the vacuum is docking, so
RvcOperationalState -- not RvcRunMode -- is the honest source for status.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re

import aiohttp
from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dreame_mcp")

BEARER = os.environ.get("MCP_BEARER_TOKEN", "")
MATTER_URL = os.environ.get("MATTER_URL", "ws://host.docker.internal:5580/ws")
NODE_ID = int(os.environ.get("MATTER_NODE_ID", "1"))
ENDPOINT = 1

RUN_MODE_CLUSTER = 84
CLEAN_MODE_CLUSTER = 85
OP_STATE_CLUSTER = 97
SERVICE_AREA_CLUSTER = 336

# RvcOperationalState.OperationalStateEnum, plus the RVC-specific values.
OP_STATES = {
    0: "stopped",
    1: "running",
    2: "paused",
    3: "error",
    64: "seeking charger",
    65: "charging",
    66: "docked",
}

# SelectAreasResponse status codes (ServiceArea cluster).
SELECT_AREAS_STATUS = {
    0: "success",
    1: "that room isn't on the vacuum's map",
    2: "the same room was listed twice",
    3: "the vacuum won't accept a room change right now -- stop it first",
}

# RvcCleanMode mode tags. 0x4001/0x4002 say whether a mode runs the vacuum,
# the mop, or both -- which is not guessable from the label. On this device
# "Auto" is vacuum+mop, "Quiet" is the only vacuum-only mode, and "AutoMop"
# is mop-only. Reading the tags instead of the labels keeps that correct even
# if a firmware update renames or reorders the modes.
MODE_TAG_VACUUM = 16385
MODE_TAG_MOP = 16386

# Phrases that describe a *job* rather than a named mode. These are resolved
# via the tags above, so they keep working regardless of what the modes are
# called.
MOP_ONLY_WORDS = {
    "mop", "mopping", "moponly", "justmop", "onlymop", "wet", "wetmop",
    "mopnovacuum", "mopwithoutvacuuming",
}
VACUUM_ONLY_WORDS = {
    "vacuumonly", "justvacuum", "onlyvacuum", "vaconly", "nomop", "dry",
    "vacuumnomop", "vacuumwithoutmopping", "suctiononly",
}

# Spoken words -> the mode label the device advertises. Anything not listed
# here still works if the user says the device's own label (e.g. "quiet").
MODE_ALIASES = {
    "vacuum": "auto",
    "vac": "auto",
    "hoover": "auto",
    "clean": "auto",
    "both": "auto",
    "vacuumandmop": "auto",
    "vacandmop": "auto",
    "deep": "deep clean",
    "eco": "low energy",
    "silent": "quiet",
    "fast": "quick",
}


class BearerAuth(BaseHTTPMiddleware):
    """Static bearer check. Pebble sends whatever header you configure."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        sent = request.headers.get("authorization", "")
        if sent != f"Bearer {BEARER}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


mcp = FastMCP("dreame-vacuum")

# The Matter controller is a local process doing real radio work; a command can
# sit for a few seconds before it answers. One connection per call keeps this
# stateless -- no socket to go stale between voice commands hours apart.
_lock = asyncio.Lock()


class MatterError(RuntimeError):
    """The Matter controller refused a command or never answered."""


# The vacuum's Matter session is not stable. Over a representative 24 hours
# this controller logged 21 subscription failures, 9 recoveries and 4 spells
# of the node being marked unavailable -- it drops every couple of hours and
# heals itself within seconds. Observed in the wild:
#
#   16:40:31  Subscription Liveness timeout
#   16:40:32  Re-Subscription succeeded
#   16:40:38  start_cleaning: rooms='bathroom'
#   16:40:45  Msg Retransmission failure (max retries: 4)
#   16:40:52  ERROR device_command: CHIP Error 0x00000032: Timeout
#
# A single attempt landed in that window and the clean never started, with
# nothing wrong on either side. Retrying is safe here because every command
# this server sends sets state rather than advancing it -- SelectAreas,
# ChangeToMode, Pause, Resume and GoHome are all idempotent, so re-sending
# one after a lost acknowledgement cannot double-apply anything.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 1.5
# Shorter than a single un-retried call would use, to keep the worst case
# (~48s) inside what a voice command can tolerate. The real failure above
# surfaced in 14s, and recovery took 1-5s, so attempt 2 nearly always wins.
ATTEMPT_TIMEOUT = 15.0

_TRANSIENT_SIGNS = (
    "timeout",
    "timed out",
    "not currently reachable",
    "unavailable",
    # "connect" rather than "connection", so this also catches aiohttp's
    # "Cannot connect to host ..." when the controller itself is restarting.
    "connect",
    "closed",
    # The prefix this module puts on any failure to reach the controller.
    "couldn't reach",
)


def _is_transient(err: Exception) -> bool:
    """Is this worth trying again, or is it a real refusal?

    Retrying a genuine rejection (an unknown node, a command the device
    doesn't accept) just delays the error the user needs to hear, so only
    connectivity-shaped failures are retried.
    """
    text = str(err).lower()
    return any(sign in text for sign in _TRANSIENT_SIGNS)


async def _with_retry(what: str, attempt_fn):
    """Run attempt_fn, retrying transient Matter failures."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await attempt_fn()
        except MatterError as err:
            if attempt == RETRY_ATTEMPTS or not _is_transient(err):
                raise
            logger.warning(
                "%s failed (attempt %d/%d): %s -- retrying in %.1fs",
                what, attempt, RETRY_ATTEMPTS, err, RETRY_BACKOFF,
            )
            await asyncio.sleep(RETRY_BACKOFF)


async def _matter(command: str, args: dict | None = None, timeout: float = 30.0):
    """Send one command to the Matter controller and return its result."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(MATTER_URL, timeout=10) as ws:
                await ws.receive_json(timeout=10)  # server info banner
                await ws.send_json(
                    {"message_id": "1", "command": command, "args": args or {}}
                )
                loop = asyncio.get_event_loop()
                deadline = loop.time() + timeout
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise MatterError(f"{command} timed out")
                    resp = await asyncio.wait_for(
                        ws.receive_json(), timeout=remaining
                    )
                    if resp.get("message_id") != "1":
                        continue  # unrelated subscription event
                    if "error_code" in resp:
                        raise MatterError(
                            resp.get("details") or f"{command} failed "
                            f"(error_code {resp['error_code']})"
                        )
                    return resp.get("result")
    except MatterError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
        raise MatterError(
            f"couldn't reach the Matter controller at {MATTER_URL}: {err}"
        ) from err


async def _attributes_once() -> dict:
    """One attempt at reading the vacuum's current attribute map."""
    nodes = await _matter("get_nodes", timeout=ATTEMPT_TIMEOUT)
    for node in nodes or []:
        if node.get("node_id") == NODE_ID:
            if not node.get("available", True):
                # Retried: the node flaps to unavailable and back within
                # seconds when its subscription drops, and reporting that as
                # "powered off" would be wrong most of the time.
                raise MatterError(
                    "the vacuum is commissioned but not currently reachable -- "
                    "it may be powered off or off the network"
                )
            return node.get("attributes", {})
    raise MatterError(
        f"no Matter node {NODE_ID} -- the vacuum may need to be re-commissioned"
    )


async def _attributes() -> dict:
    """Return the vacuum's current attribute map."""
    return await _with_retry("reading vacuum state", _attributes_once)


async def _device_command(cluster: int, name: str, payload: dict | None = None):
    async def attempt():
        return await _matter(
            "device_command",
            {
                "node_id": NODE_ID,
                "endpoint_id": ENDPOINT,
                "cluster_id": cluster,
                "command_name": name,
                "payload": payload or {},
            },
            timeout=ATTEMPT_TIMEOUT,
        )

    return await _with_retry(f"{name} on cluster {cluster}", attempt)


def _norm(text: str) -> str:
    """Fold spoken room/mode names onto the device's own spelling.

    The device names rooms with no spaces ("livingroom", "bathroom2") while a
    person says "living room" and "bathroom two". Stripping non-alphanumerics
    and mapping number words closes most of that gap before fuzzy matching
    has to guess.
    """
    text = text.lower().strip()
    for word, digit in (
        ("one", "1"), ("two", "2"), ("three", "3"),
        ("four", "4"), ("five", "5"),
    ):
        text = re.sub(rf"\b{word}\b", digit, text)
    return re.sub(r"[^a-z0-9]", "", text)


def _areas(attrs: dict) -> dict[int, str]:
    """Map area ID -> room name, from the device's own map.

    Read this fresh on every call and never cache the IDs. Editing the map in
    the Dreame app renumbers areas wholesale -- observed on this device:
    renaming one room moved bathroom from ID 1 to 4, corridor from 2 to 6,
    and bedroom from 3 to 5. A cached ID would quietly clean the wrong room,
    with nothing in the response to hint that anything was wrong.
    """
    out: dict[int, str] = {}
    for entry in attrs.get(f"{ENDPOINT}/{SERVICE_AREA_CLUSTER}/0") or []:
        area_id = entry.get("0")
        # areaInfo.locationInfo.locationName
        name = ((entry.get("2") or {}).get("0") or {}).get("0")
        if area_id is not None and name:
            out[area_id] = name
    return out


def _modes(attrs: dict, cluster: int) -> dict[int, str]:
    """Map mode value -> label, for a RvcRunMode/RvcCleanMode style cluster."""
    out: dict[int, str] = {}
    for entry in attrs.get(f"{ENDPOINT}/{cluster}/0") or []:
        label, value = entry.get("0"), entry.get("1")
        if label is not None and value is not None:
            out[value] = label
    return out


def _resolve_rooms(spoken: str, areas: dict[int, str]) -> list[int]:
    """Turn "kitchen and the living room" into [7, 4]."""
    known = {_norm(name): aid for aid, name in areas.items()}
    chosen: list[int] = []
    unmatched: list[str] = []

    parts = [p for p in re.split(r",| and | & |\+", spoken) if p.strip()]
    for part in parts:
        key = _norm(part)
        if not key:
            continue
        # Strip filler the ring tends to pick up ("the kitchen", "in the den").
        key = re.sub(r"^(the|in|my|our)", "", key) or key
        aid = known.get(key)
        if aid is None:
            close = difflib.get_close_matches(key, known.keys(), n=1, cutoff=0.7)
            aid = known[close[0]] if close else None
        if aid is None:
            unmatched.append(part.strip())
        elif aid not in chosen:
            chosen.append(aid)

    if unmatched:
        raise ValueError(
            f"I don't have a room called {', '.join(unmatched)}. "
            f"The map has: {', '.join(sorted(areas.values()))}."
        )
    return chosen


def _mode_tags(attrs: dict, cluster: int) -> dict[int, set[int]]:
    """Map mode value -> its set of mode tags."""
    out: dict[int, set[int]] = {}
    for entry in attrs.get(f"{ENDPOINT}/{cluster}/0") or []:
        value = entry.get("1")
        if value is None:
            continue
        out[value] = {
            tag.get("1")
            for tag in (entry.get("2") or [])
            if isinstance(tag, dict) and tag.get("1") is not None
        }
    return out


def _mode_jobs(tags: set[int]) -> str:
    """Describe a mode as 'vacuum', 'mop', 'vacuum + mop', or ''."""
    jobs = []
    if MODE_TAG_VACUUM in tags:
        jobs.append("vacuum")
    if MODE_TAG_MOP in tags:
        jobs.append("mop")
    return " + ".join(jobs)


def _resolve_mode(
    spoken: str, modes: dict[int, str], tags: dict[int, set[int]] | None = None
) -> int:
    """Turn "mop" into the RvcCleanMode value that mops but doesn't vacuum.

    Job phrases ("mop", "vacuum only") are resolved from the mode tags rather
    than the labels, since the labels don't say what a mode actually does --
    "Auto" runs both the vacuum and the mop on this device.
    """
    key = _norm(spoken)
    tags = tags or {}

    if key in MOP_ONLY_WORDS:
        match = next(
            (v for v, t in tags.items()
             if MODE_TAG_MOP in t and MODE_TAG_VACUUM not in t), None
        )
        if match is not None:
            return match
    if key in VACUUM_ONLY_WORDS:
        match = next(
            (v for v, t in tags.items()
             if MODE_TAG_VACUUM in t and MODE_TAG_MOP not in t), None
        )
        if match is not None:
            return match

    key = _norm(MODE_ALIASES.get(key, key))
    known = {_norm(label): value for value, label in modes.items()}
    if key in known:
        return known[key]
    close = difflib.get_close_matches(key, known.keys(), n=1, cutoff=0.6)
    if close:
        return known[close[0]]
    raise ValueError(
        f"I don't know a cleaning mode called '{spoken}'. "
        f"Options: {', '.join(sorted(modes.values()))}."
    )


def _describe(attrs: dict) -> str:
    """One spoken-friendly sentence about what the vacuum is doing."""
    op = attrs.get(f"{ENDPOINT}/{OP_STATE_CLUSTER}/4")
    state = OP_STATES.get(op, f"state {op}")
    areas = _areas(attrs)
    current = attrs.get(f"{ENDPOINT}/{SERVICE_AREA_CLUSTER}/3")
    selected = attrs.get(f"{ENDPOINT}/{SERVICE_AREA_CLUSTER}/2") or []
    clean_modes = _modes(attrs, CLEAN_MODE_CLUSTER)
    mode_value = attrs.get(f"{ENDPOINT}/{CLEAN_MODE_CLUSTER}/1")
    clean_now = clean_modes.get(mode_value)
    if clean_now:
        jobs = _mode_jobs(_mode_tags(attrs, CLEAN_MODE_CLUSTER).get(mode_value, set()))
        if jobs:
            clean_now = f"{clean_now} ({jobs})"

    if op == 3:
        # Don't dress an error up with mode chatter -- the error line below
        # is the only part that matters here.
        sentence = "The vacuum has stopped with a problem."
    else:
        parts = [f"The vacuum is {state}"]
        if op == 1 and current in areas:
            parts.append(f"in the {areas[current]}")
        if clean_now:
            parts.append(f"in {clean_now} mode")
        sentence = " ".join(parts) + "."

    # Only mention queued rooms while a job is actually live. The device keeps
    # the last job's selection forever once it's docked, and reporting that as
    # "queued" would imply the next clean is limited to those rooms -- which is
    # wrong, since start_cleaning with no rooms explicitly selects every area.
    if selected and op in (1, 2):
        names = [areas.get(a, str(a)) for a in selected]
        sentence += f" Rooms queued: {', '.join(names)}."

    err = attrs.get(f"{ENDPOINT}/{OP_STATE_CLUSTER}/5") or {}
    err_id = err.get("0")
    if err_id:
        label = err.get("1") or f"error {err_id}"
        sentence += f" It's reporting an error: {label}."
    return sentence


@mcp.tool
async def vacuum_status() -> str:
    """Report what the robot vacuum is doing right now.

    Use this for any question about the vacuum's current state -- whether
    it's running, docked, charging, stuck, or which room it's in.
    """
    logger.info("vacuum_status: called")
    async with _lock:
        try:
            return _describe(await _attributes())
        except MatterError as err:
            return f"Couldn't reach the vacuum: {err}"


@mcp.tool
async def list_rooms() -> str:
    """List the rooms the vacuum can clean, from its own saved map.

    These names come from the map configured in the Dreame app, so they are
    the only names `start_cleaning` will accept.
    """
    logger.info("list_rooms: called")
    async with _lock:
        try:
            areas = _areas(await _attributes())
        except MatterError as err:
            return f"Couldn't reach the vacuum: {err}"
    if not areas:
        return "The vacuum doesn't report any rooms -- its map may not be set up yet."
    return "Rooms on the map: " + ", ".join(sorted(areas.values())) + "."


@mcp.tool
async def list_modes() -> str:
    """List the vacuum's cleaning modes and whether each vacuums, mops, or both.

    Use this when the user asks what modes exist, or what the difference
    between them is.
    """
    logger.info("list_modes: called")
    async with _lock:
        try:
            attrs = await _attributes()
        except MatterError as err:
            return f"Couldn't reach the vacuum: {err}"
    modes = _modes(attrs, CLEAN_MODE_CLUSTER)
    tags = _mode_tags(attrs, CLEAN_MODE_CLUSTER)
    current = attrs.get(f"{ENDPOINT}/{CLEAN_MODE_CLUSTER}/1")
    if not modes:
        return "The vacuum didn't report any cleaning modes."
    lines = []
    for value, label in sorted(modes.items()):
        jobs = _mode_jobs(tags.get(value, set())) or "unspecified"
        lines.append(f"{label} ({jobs})" + (" -- current" if value == current else ""))
    return "Cleaning modes: " + "; ".join(lines) + "."


@mcp.tool
async def start_cleaning(rooms: str = "", mode: str = "") -> str:
    """Start the robot vacuum cleaning.

    Args:
        rooms: Which rooms to clean, as the user said them, e.g. "kitchen"
            or "kitchen and the living room". Leave as an empty string to
            clean the whole home. Names are matched against the vacuum's own
            map, so slight differences ("living room" vs "livingroom") are
            fine, but a room that isn't on the map is reported back rather
            than guessed at.
        mode: How to clean, e.g. "vacuum", "mop", "deep", "quiet", "quick".
            Leave as an empty string to keep whatever mode it's already set
            to. Say "mop" for mopping; plain "vacuum" means normal suction.

    IMPORTANT: if the result describes an error or says a room wasn't found,
    stop and relay that message to the user verbatim. Do not retry with a
    different room name, and do not invent a room -- the user needs to hear
    which names actually exist.
    """
    logger.info("start_cleaning: rooms=%r mode=%r", rooms, mode)
    async with _lock:
        try:
            attrs = await _attributes()

            all_areas = _areas(attrs)
            if rooms.strip():
                area_ids = _resolve_rooms(rooms, all_areas)
                whole_home = False
            else:
                # "Clean everything" is sent as an explicit list of every room
                # rather than the empty list the spec defines as "no area
                # limits". The empty list is accepted (status 0) and does take
                # effect, but this firmware restores the previous selection
                # within ~10 seconds -- so a whole-home request could quietly
                # collapse back to the last room-specific one. Naming every
                # area leaves nothing to revert to.
                area_ids = sorted(all_areas)
                whole_home = True

            mode_value = None
            if mode.strip():
                mode_value = _resolve_mode(
                    mode,
                    _modes(attrs, CLEAN_MODE_CLUSTER),
                    _mode_tags(attrs, CLEAN_MODE_CLUSTER),
                )

            # Always set the selection. The device keeps the last one
            # indefinitely, so skipping this would let a previous "clean the
            # kitchen" silently narrow a later whole-home request down to just
            # the kitchen -- with the reply still claiming it cleaned
            # everything.
            #
            # Rooms first: the device rejects a selection change once it is
            # already running, so this has to land before the start command.
            result = await _device_command(
                SERVICE_AREA_CLUSTER, "SelectAreas", {"newAreas": area_ids}
            )
            status = (result or {}).get("status")
            if status:
                why = SELECT_AREAS_STATUS.get(status, f"status {status}")
                return f"Couldn't set those rooms: {why}."

            if mode_value is not None:
                await _device_command(
                    CLEAN_MODE_CLUSTER, "ChangeToMode", {"newMode": mode_value}
                )

            run_modes = _modes(attrs, RUN_MODE_CLUSTER)
            cleaning = next(
                (v for v, label in run_modes.items() if _norm(label) == "cleaning"),
                1,
            )
            await _device_command(
                RUN_MODE_CLUSTER, "ChangeToMode", {"newMode": cleaning}
            )
        except (ValueError, MatterError) as err:
            return str(err)

    if whole_home:
        where = f"the whole home ({len(area_ids)} rooms)" if area_ids else "the whole home"
    else:
        where = ", ".join(all_areas.get(a, str(a)) for a in area_ids)
    how = ""
    if mode_value is not None:
        label = _modes(attrs, CLEAN_MODE_CLUSTER)[mode_value]
        jobs = _mode_jobs(_mode_tags(attrs, CLEAN_MODE_CLUSTER).get(mode_value, set()))
        how = f" in {label} mode" + (f" ({jobs})" if jobs else "")
    return f"Started cleaning {where}{how}."


@mcp.tool
async def stop_cleaning() -> str:
    """Stop the robot vacuum and send it back to its dock.

    Use this for "stop", "stop cleaning", or "that's enough". The vacuum
    ends the job and returns to the dock on its own.
    """
    logger.info("stop_cleaning: called")
    async with _lock:
        try:
            attrs = await _attributes()
            run_modes = _modes(attrs, RUN_MODE_CLUSTER)
            idle = next(
                (v for v, label in run_modes.items() if _norm(label) == "idle"), 0
            )
            await _device_command(RUN_MODE_CLUSTER, "ChangeToMode", {"newMode": idle})
        except MatterError as err:
            return f"Couldn't stop the vacuum: {err}"
    return "Stopped cleaning. The vacuum is heading back to its dock."


@mcp.tool
async def dock() -> str:
    """Send the robot vacuum back to its charging dock.

    Use this for "go home", "dock", "go charge". Works whether or not it is
    currently cleaning.
    """
    logger.info("dock: called")
    async with _lock:
        try:
            attrs = await _attributes()
            # Idle first -- GoHome alone is ACKed but ignored mid-clean on
            # this firmware, so without this the vacuum just keeps going.
            run_modes = _modes(attrs, RUN_MODE_CLUSTER)
            idle = next(
                (v for v, label in run_modes.items() if _norm(label) == "idle"), 0
            )
            await _device_command(RUN_MODE_CLUSTER, "ChangeToMode", {"newMode": idle})
            await _device_command(OP_STATE_CLUSTER, "GoHome")
        except MatterError as err:
            return f"Couldn't send the vacuum home: {err}"
    return "Sending the vacuum back to its dock."


@mcp.tool
async def pause_cleaning() -> str:
    """Pause the robot vacuum where it is, without sending it to the dock.

    Use this for "pause" or "hold on". Resume with resume_cleaning.
    """
    logger.info("pause_cleaning: called")
    async with _lock:
        try:
            await _device_command(OP_STATE_CLUSTER, "Pause")
        except MatterError as err:
            return f"Couldn't pause the vacuum: {err}"
    return "Paused."


@mcp.tool
async def resume_cleaning() -> str:
    """Resume the robot vacuum after it was paused.

    Use this for "resume", "keep going", or "carry on".
    """
    logger.info("resume_cleaning: called")
    async with _lock:
        try:
            await _device_command(OP_STATE_CLUSTER, "Resume")
        except MatterError as err:
            return f"Couldn't resume the vacuum: {err}"
    return "Resuming."


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = mcp.http_app(path="/mcp")
app.add_middleware(BearerAuth)
app.router.routes.insert(0, Route("/healthz", healthz))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
