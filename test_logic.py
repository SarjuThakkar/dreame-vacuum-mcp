"""Test the pure parsing/matching logic against real captured device data.

No network, no device, no Matter controller needed -- the fixtures below are
a real attribute payload read off a Dreame Matrix 10 (firmware 4.3.9_3835).

    python3 test_logic.py
"""
import sys
import types

# Stub the runtime-only deps so the module imports without aiohttp/fastmcp.
for name in ("aiohttp",):
    sys.modules[name] = types.ModuleType(name)

fastmcp = types.ModuleType("fastmcp")
class _FastMCP:
    def __init__(self, *a, **k): pass
    def tool(self, fn): return fn
    def http_app(self, **k):
        class _App:
            router = types.SimpleNamespace(routes=[])
            def add_middleware(self, *a, **k): pass
        return _App()
fastmcp.FastMCP = _FastMCP
sys.modules["fastmcp"] = fastmcp

for mod, attrs in (
    ("starlette.middleware.base", {"BaseHTTPMiddleware": object}),
    ("starlette.requests", {"Request": object}),
    ("starlette.responses", {"JSONResponse": lambda *a, **k: None}),
    ("starlette.routing", {"Route": lambda *a, **k: None}),
):
    m = types.ModuleType(mod)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[mod] = m
sys.modules.setdefault("starlette", types.ModuleType("starlette"))
sys.modules.setdefault("starlette.middleware", types.ModuleType("starlette.middleware"))

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dreame_mcp_server as d

# --- Real attribute payload captured from the device (node 1, endpoint 1) ---
ATTRS = {
    "1/84/0": [{"0": "Idle", "1": 0, "2": [{"1": 16384}]},
               {"0": "Cleaning", "1": 1, "2": [{"1": 16385}]},
               {"0": "Mapping", "1": 2, "2": [{"1": 16386}]}],
    "1/84/1": 0,
    "1/85/0": [
        {"0": "Quick", "1": 0, "2": [{"1": 1}, {"1": 16385}, {"1": 16386}]},
        {"0": "Auto", "1": 1, "2": [{"1": 0}, {"1": 16385}, {"1": 16386}]},
        {"0": "Deep Clean", "1": 2, "2": [{"1": 16384}, {"1": 16386}, {"1": 16385}]},
        {"0": "Quiet", "1": 3, "2": [{"1": 2}, {"1": 16385}]},
        {"0": "Low Energy", "1": 4, "2": [{"1": 16385}, {"1": 16386}, {"1": 2}]},
        {"0": "AutoMop", "1": 5, "2": [{"1": 0}, {"1": 16386}]},
    ],
    "1/85/1": 1,
    "1/97/4": 66,
    "1/97/5": {"0": 0},
    "1/336/0": [
        {"0": 1, "1": 2, "2": {"0": {"0": "bathroom", "1": 1, "2": 6}, "1": None}},
        {"0": 2, "1": 2, "2": {"0": {"0": "corridor", "1": 1, "2": 16}, "1": None}},
        {"0": 3, "1": 2, "2": {"0": {"0": "bedroom", "1": 1, "2": 7}, "1": None}},
        {"0": 4, "1": 2, "2": {"0": {"0": "livingroom", "1": 1, "2": 50}, "1": None}},
        {"0": 5, "1": 2, "2": {"0": {"0": "bathroom2", "1": 1, "2": 6}, "1": None}},
        {"0": 6, "1": 2, "2": {"0": {"0": "bedroom2", "1": 1, "2": 7}, "1": None}},
        {"0": 7, "1": 2, "2": {"0": {"0": "kitchen", "1": 1, "2": 46}, "1": None}},
    ],
    "1/336/2": [],
    "1/336/3": None,
}

failures = []

def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {got!r}")
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")

areas = d._areas(ATTRS)
check("parse areas", areas,
      {1: "bathroom", 2: "corridor", 3: "bedroom", 4: "livingroom",
       5: "bathroom2", 6: "bedroom2", 7: "kitchen"})

clean_modes = d._modes(ATTRS, 85)
check("parse clean modes", clean_modes,
      {0: "Quick", 1: "Auto", 2: "Deep Clean", 3: "Quiet",
       4: "Low Energy", 5: "AutoMop"})

print("\n--- room resolution ---")
check("kitchen", d._resolve_rooms("kitchen", areas), [7])
check("living room (spaced)", d._resolve_rooms("living room", areas), [4])
check("the kitchen (filler)", d._resolve_rooms("the kitchen", areas), [7])
check("kitchen and living room", d._resolve_rooms("kitchen and the living room", areas), [7, 4])
check("comma list", d._resolve_rooms("bedroom, kitchen", areas), [3, 7])
check("bedroom two -> bedroom2", d._resolve_rooms("bedroom two", areas), [6])
check("bathroom 2 (digit+space)", d._resolve_rooms("bathroom 2", areas), [5])
check("dedupe repeats", d._resolve_rooms("kitchen and kitchen", areas), [7])
check("typo tolerance", d._resolve_rooms("kitcen", areas), [7])

print("\n--- unknown room must raise, not guess ---")
try:
    d._resolve_rooms("garage", areas)
    check("unknown room raises", False, True)
except ValueError as e:
    print(f"PASS  unknown room raises: {e}")

tagmap = d._mode_tags(ATTRS, 85)
check("parse mode tags (Auto)", tagmap[1], {0, 16385, 16386})
check("Auto does vacuum+mop", d._mode_jobs(tagmap[1]), "vacuum + mop")
check("Quiet does vacuum only", d._mode_jobs(tagmap[3]), "vacuum")
check("AutoMop does mop only", d._mode_jobs(tagmap[5]), "mop")

print("\n--- mode resolution (tag-driven) ---")
check("mop -> AutoMop(5), mop-only", d._resolve_mode("mop", clean_modes, tagmap), 5)
check("just mop -> AutoMop(5)", d._resolve_mode("just mop", clean_modes, tagmap), 5)
check("vacuum only -> Quiet(3)", d._resolve_mode("vacuum only", clean_modes, tagmap), 3)
check("no mop -> Quiet(3)", d._resolve_mode("no mop", clean_modes, tagmap), 3)
check("vacuum -> Auto(1) vac+mop", d._resolve_mode("vacuum", clean_modes, tagmap), 1)
check("vac -> Auto(1)", d._resolve_mode("vac", clean_modes, tagmap), 1)
check("both -> Auto(1)", d._resolve_mode("both", clean_modes, tagmap), 1)
check("deep -> Deep Clean(2)", d._resolve_mode("deep", clean_modes, tagmap), 2)
check("deep clean literal", d._resolve_mode("deep clean", clean_modes, tagmap), 2)
check("quiet literal", d._resolve_mode("quiet", clean_modes, tagmap), 3)
check("eco -> Low Energy(4)", d._resolve_mode("eco", clean_modes, tagmap), 4)
check("automop literal", d._resolve_mode("automop", clean_modes, tagmap), 5)

try:
    d._resolve_mode("turbo blast", clean_modes, tagmap)
    check("unknown mode raises", False, True)
except ValueError as e:
    print(f"PASS  unknown mode raises: {e}")

print("\n--- status sentences ---")
print("docked :", d._describe(ATTRS))
running = dict(ATTRS, **{"1/97/4": 1, "1/336/3": 7, "1/336/2": [7, 4]})
print("running:", d._describe(running))
errored = dict(ATTRS, **{"1/97/4": 3, "1/97/5": {"0": 5, "1": "Stuck"}})
print("error  :", d._describe(errored))

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All assertions passed.")
