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
    area: Tuple[Tuple[float, float], Tuple[float, float]] = ((-30, -30), (30, 30)),
    surface: str = "nauvis",
) -> str:
    """Export entities in the given area as a Factorio blueprint string.
    Returns the raw '0eN...' string. Uses a scratch inventory — no player needed.
    """
    (x1, y1), (x2, y2) = area
    cmd = (
        "/sc local inv=game.create_inventory(1) "
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
    return (rcon.send_command(cmd) or "").strip()


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
