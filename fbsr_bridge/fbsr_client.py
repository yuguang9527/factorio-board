"""Client helpers for pushing FBSR render jobs to Redis.

Usage from agent_loop.py:
    from fbsr_bridge.fbsr_client import enqueue_render, export_blueprint
    bp = export_blueprint(rcon, area=((-30, -30), (30, 30)))
    job_id = enqueue_render(bp, out_path="/path/to/step_0001.png")
"""
import json
import os
import uuid
from typing import Optional, Tuple

import redis


def _redis() -> redis.Redis:
    return redis.Redis(decode_responses=True)


def export_blueprint(
    rcon,
    area: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None,
    surface: str = "nauvis",
    padding: float = 5.0,
) -> str:
    """Export entities as a Factorio blueprint string.
    If `area` is None (default), auto-fit to the bounding box of all player-force
    entities (+ padding). Falls back to a small origin window if no entities exist.
    Uses a scratch inventory — no player needed.
    """
    if area is None:
        # Auto-compute bounding box from live entities
        bbox = _cmd_raw(rcon,
            f"local es=game.surfaces['{surface}'].find_entities_filtered{{force='player'}} "
            "local n=0 local x1,y1,x2,y2=math.huge,math.huge,-math.huge,-math.huge "
            "for _,e in pairs(es) do "
            "  if e.name~='character' and not e.name:find('crash') then "
            "    n=n+1 "
            "    if e.position.x<x1 then x1=e.position.x end "
            "    if e.position.y<y1 then y1=e.position.y end "
            "    if e.position.x>x2 then x2=e.position.x end "
            "    if e.position.y>y2 then y2=e.position.y end "
            "  end "
            "end "
            "if n==0 then rcon.print('empty') "
            "else rcon.print(x1..','..y1..','..x2..','..y2) end"
        )
        if not bbox or bbox == "empty":
            (x1, y1), (x2, y2) = (-10, -10), (10, 10)
        else:
            x1, y1, x2, y2 = [float(v) for v in bbox.split(",")]
            x1 -= padding; y1 -= padding; x2 += padding; y2 += padding
    else:
        (x1, y1), (x2, y2) = area

    cmd = (
        "local inv=game.create_inventory(1) "
        "local stack=inv[1] "
        "stack.set_stack{name='blueprint',count=1} "
        "stack.create_blueprint{"
        f"surface=game.surfaces['{surface}'],"
        "force='player',"
        f"area={{{{{x1},{y1}}},{{{x2},{y2}}}}},"
        "always_include_tiles=false} "
        "local s=stack.export_stack() "
        "inv.destroy() "
        "rcon.print(s)"
    )
    return _cmd_raw(rcon, cmd)


def _cmd_raw(rcon, lua: str) -> str:
    return (rcon.send_command("/sc " + lua) or "").strip()


def enqueue_render(
    bp_string: str,
    out_path: str,
    job_id: Optional[str] = None,
    queue: str = "fbsr:jobs",
) -> str:
    """Push a render job to Redis. Returns the job id."""
    if not bp_string:
        raise ValueError("empty blueprint string")
    jid = job_id or uuid.uuid4().hex[:12]
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    payload = {"id": jid, "bp": bp_string, "out": out_path}
    _redis().lpush(queue, json.dumps(payload))
    return jid


def wait_for_render(job_id: str, timeout: float = 30.0) -> Optional[str]:
    """Block until the worker reports completion, or timeout. Returns worker status."""
    r = _redis()
    import time
    t0 = time.time()
    key = f"fbsr:done:{job_id}"
    while time.time() - t0 < timeout:
        v = r.get(key)
        if v is not None:
            return v
        time.sleep(0.1)
    return None
