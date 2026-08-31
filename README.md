# dreame-vacuum-mcp

Voice control for a Dreame robot vacuum from a [Pebble Index](https://www.pebble.computer/) ring,
over **Matter** — entirely on the local network, no vendor cloud in the path.

Say *"clean the kitchen"* or *"mop the living room"* and the vacuum goes.

```
Pebble ring  ──MCP/HTTPS──▶  this server  ──WebSocket──▶  matter-server  ──Matter──▶  vacuum
 (voice)                      (Pi, :8003)                  (Pi, :5580)                (LAN)
```

Runs on the same Raspberry Pi as the rest of my home services — see
[pi-home-services](https://github.com/SarjuThakkar/pi-home-services) for the
Docker Compose orchestration and Cloudflare Tunnel setup.

## Why Matter

The Dreame Matrix 10 isn't in the community [`dreame-vacuum`](https://github.com/Tasshack/dreame-vacuum)
integration's supported-device list, and Dreame publishes no official HTTP API.
Matter turned out to be the better path anyway:

- **Vendor-sanctioned.** No reverse-engineered cloud endpoints to break.
- **Local.** No round-trip through Dreame's servers; works if the internet is down.
- **It has the rooms.** The `ServiceArea` cluster (Matter 1.4+) exposes the actual
  room map drawn in the Dreame app, so per-room cleaning works without
  re-describing the floorplan anywhere.

Dreame's own forum said Matter was "still under verification" as recently as
mid-2026, but it works on this unit (firmware `4.3.9_3835`) — the pairing code
is in the app under the Matter section.

## Tools

| Tool | What it does |
|---|---|
| `vacuum_status` | What it's doing now — running, docked, charging, which room, any error |
| `list_rooms` | Room names from the vacuum's own map |
| `list_modes` | Cleaning modes, and whether each vacuums, mops, or both |
| `start_cleaning(rooms, mode)` | Start. Both args optional: no rooms = whole home, no mode = leave as-is |
| `stop_cleaning` | End the job; vacuum returns to the dock |
| `dock` | Send it home |
| `pause_cleaning` / `resume_cleaning` | Pause in place / carry on |

Rooms and modes are matched against whatever the device currently reports, so
`"living room"` finds `livingroom`, `"bedroom two"` finds `bedroom2`, and a
typo like `"kitcen"` still resolves. A room that genuinely isn't on the map is
reported back with the real list rather than guessed at.

### Cleaning modes

A mode's label doesn't say what it actually does — `Auto` runs *both* the
vacuum and the mop. The Matter mode tags (`0x4001` Vacuum, `0x4002` Mop) do,
so this server reads those rather than pattern-matching names:

| Mode | Actually does |
|---|---|
| Quick | vacuum + mop |
| **Auto** (device default) | **vacuum + mop** |
| Deep Clean | vacuum + mop |
| Quiet | vacuum only |
| Low Energy | vacuum + mop |
| AutoMop | mop only |

So `"mop"` resolves to the mode that mops but doesn't vacuum, and
`"vacuum only"` / `"no mop"` to the one that vacuums but doesn't mop —
picked from the tags, so they stay correct if firmware renames the modes.
Plain `"vacuum"` means normal cleaning (`Auto`, i.e. both), which is what
people usually mean by "vacuum the kitchen". Bare mode names
(`"deep clean"`, `"quiet"`) work too.

## Matter clusters used

All on endpoint 1. The accepted-command lists are what this device actually
reports, which is narrower than what the spec permits:

| Cluster | Commands accepted | Used for |
|---|---|---|
| 84 `RvcRunMode` | `ChangeToMode` | start (Cleaning) / stop (Idle) |
| 85 `RvcCleanMode` | `ChangeToMode` | vacuum vs mop |
| 97 `RvcOperationalState` | `Pause`, `Resume`, `GoHome` | pause/resume/dock |
| 336 `ServiceArea` | `SelectAreas` | room targeting |

Note there is no `Stop`/`Start` on cluster 97 — stopping goes through
`RvcRunMode` instead.

## Device quirks found while building this

Real behaviour on firmware `4.3.9_3835`, all verified against the physical unit:

- **`GoHome` mid-clean is acknowledged but ignored.** It returns
  `errorStateID: 0` (success) and the vacuum just keeps cleaning. What actually
  ends a job is `RvcRunMode → Idle`, after which it heads to the dock on its
  own. So `dock` sets Idle *first*, then sends `GoHome`.
- **`RvcRunMode` lies once docking starts.** It keeps reporting `Cleaning`
  while the vacuum is already `SeekingCharger`. `RvcOperationalState` is the
  honest source, so status reads from that.
- **Area IDs are not stable.** Editing the map in the Dreame app renumbers
  areas wholesale — renaming a single room moved `bathroom` from ID 1 to 4,
  `corridor` from 2 to 6, `bedroom` from 3 to 5. Anything caching area IDs
  would silently start cleaning the *wrong room*, with nothing in the response
  to indicate a problem. This server therefore resolves room names to IDs
  fresh on every call and never caches them.
- **`SelectAreas` is rejected while running** (status 3, `InvalidInMode`), so
  room selection has to land before the start command, not after.
- **The area selection is sticky, and an empty selection won't stick.** The
  selection persists after a job ends, so a plain "start cleaning" following
  an earlier "clean the kitchen" would quietly clean *only the kitchen* while
  reporting it was cleaning the whole home. The spec's fix — `SelectAreas([])`,
  meaning "no area limits" — is accepted (`status 0`) and does take effect,
  but this firmware **restores the previous selection within ~10 seconds**:

  ```
  SelectAreas([]) -> {"status": 0}
  t+0s:  SelectedAreas = []
  t+10s: SelectedAreas = [7]     <- reverted on its own
  ```

  So a whole-home clean is sent as an explicit list of *every* area ID
  instead. There is then no earlier selection left to revert to.

## Setup

Requires a running Matter controller with the vacuum already commissioned.
See [pi-home-services](https://github.com/SarjuThakkar/pi-home-services) for
the `matter-server` container; commissioning is a one-time
`commission_with_code` call with the pairing code from the Dreame app.

The Matter controller's host needs **IPv6 enabled on the LAN interface** —
Matter runs on IPv6 link-local multicast and simply will not discover devices
without it. On the Pi this meant adding `dhcp6: true` / `accept-ra: true` to
the wlan0 netplan config, which had IPv6 off entirely.

```bash
python3 test_logic.py     # offline tests, no device needed
cp .env.example .env      # fill in MCP_BEARER_TOKEN, MATTER_NODE_ID
docker build -t dreame-mcp .
docker run -p 8003:8000 --env-file .env \
  --add-host host.docker.internal:host-gateway dreame-mcp
```

Point the Pebble app at `https://<your-host>/mcp` with the bearer token.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MCP_BEARER_TOKEN` | — | Static token Pebble sends as `Authorization: Bearer <token>` |
| `MATTER_URL` | `ws://host.docker.internal:5580/ws` | Matter controller WebSocket |
| `MATTER_NODE_ID` | `1` | Node ID assigned to the vacuum at commissioning |
| `PORT` | `8000` | Port inside the container |
