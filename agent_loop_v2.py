#!/usr/bin/env python3
"""Agent loop v2 — Python REPL with intention-level tools.

Each turn the LLM writes a short Python program that uses the `world` facade
from tools.py. Program runs, stdout is captured, state + stdout feed back.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from contextlib import redirect_stdout

import litellm
from factorio_rcon import RCONClient

from tools import World, Pos, Ent, Direction  # noqa: F401 (exposed to LLM)

# ── Config ──────────────────────────────────────────────────────────
PIPE_PATH = os.environ.get(
    "FACTORIO_PIPE_PATH",
    os.path.expanduser(
        "~/Library/Application Support/factorio/script-output/events.pipe"
    ),
)
RCON_HOST = os.environ.get("FACTORIO_HOST", "localhost")
RCON_PORT = int(os.environ.get("FACTORIO_RCON_PORT", "27015"))
RCON_PASS = os.environ.get("FACTORIO_RCON_PASSWORD", "factorio")
MODEL = os.environ.get("AGENT_MODEL", "cc/claude-sonnet-4-20250514")
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "20"))
TASK = os.environ.get("AGENT_TASK", "produce_iron_plates")
SCREENSHOT_DIR = os.environ.get(
    "FACTORIO_OUTPUT_PATH",
    os.path.expanduser("~/Library/Application Support/factorio/script-output"),
)
WEAVE_ENTITY = os.environ.get("WEAVE_ENTITY", "weave-trace-move")
WEAVE_PROJECT = os.environ.get("WEAVE_PROJECT", "factorio-experiments")

ENTITY_NAMES = [
    "burner-mining-drill", "stone-furnace", "burner-inserter", "inserter",
    "wooden-chest", "iron-chest", "steel-chest",
    "assembling-machine-1", "assembling-machine-2", "assembling-machine-3",
    "transport-belt", "fast-transport-belt",
    "boiler", "steam-engine", "pipe", "offshore-pump", "small-electric-pole",
]
RESOURCE_NAMES = ["iron-ore", "copper-ore", "coal", "stone"]

SYSTEM_PROMPT = f"""\
You are a Factorio strategist running in cheat mode (all tech unlocked, infinite
inventory, entities spawn directly). Every turn you write a short Python program
that runs against a live headless Factorio server.

The LAST return value in your globals — or whatever you print — feeds back next turn.

## Available tools (methods of `world`, an instance of World)

```python
world.nearest(resource: str, origin: Pos = Pos(0,0), radius: int = 200) -> Pos | None
world.place(name: str, at: Pos, direction: Direction = 'north') -> Ent | None
world.place_next_to(name: str, ref: Ent, direction: Direction,
                    facing: Direction | None = None) -> Ent | None
world.connect_belt(src: Pos, dst: Pos, belt: str = 'transport-belt') -> int
world.insert(entity: Ent, item: str, count: int) -> int   # returns count actually inserted
world.inventory(entity: Ent) -> dict[str, int]
world.get_entities(within: float = 50, origin: Pos = Pos(0,0)) -> list[Ent]
world.score() -> float   # cumulative items produced on nauvis
```

Plus helpers in globals: `Pos`, `Ent`, `Direction`. No imports needed.

## Entity names (strings)
{', '.join(ENTITY_NAMES)}

## Resource names (strings for world.nearest)
{', '.join(RESOURCE_NAMES)}

## Direction
'north' | 'south' | 'east' | 'west'

## GOAL
{TASK} — maximise `world.score()` by building a **self-sustaining production chain**.

## Strategy
1. Find iron-ore with `world.nearest('iron-ore')`. Note: origin is (0,0) by default.
2. Place a burner-mining-drill ON the ore, facing the direction you want output.
3. Place a furnace via `place_next_to(drill, direction='south')` so drill output
   feeds furnace directly — drills deposit into the tile adjacent to their output.
4. Place a burner-inserter next to furnace output, place a chest on the other side.
5. `world.insert(drill, 'coal', 50)` and `world.insert(furnace, 'coal', 50)`.
6. Run `world.score()` and `world.inventory(chest)` after a few turns to verify.

## Rules
- ONE Python program per turn, wrapped in a ```python block.
- `print()` anything you want to remember — you see stdout next turn.
- Return values from placement are `Ent | None`. Always check for None.
- Don't hard-code unit numbers; keep returned Ent objects as variables across turns
  by printing them, then re-locating via `get_entities` if needed. Variables reset
  each turn — only printed output + world state persist.
- `connect_belt(src, dst)` routes an L-shaped belt. No obstacle avoidance.
- Burner drills/furnaces need coal in their fuel slot to work.
"""


def write_event(pipe, event: dict):
    if pipe:
        try:
            pipe.write(json.dumps(event, ensure_ascii=False) + "\n")
            pipe.flush()
        except BrokenPipeError:
            pass


def ensure_pipe():
    try:
        fd = os.open(PIPE_PATH, os.O_WRONLY | os.O_NONBLOCK)
    except (FileNotFoundError, OSError) as e:
        print(f"⚠️  Pipe not connectable ({e}). Weave tracing disabled.")
        return None
    return os.fdopen(fd, "w")


def take_screenshot(rcon, session_id: str, step: int) -> str | None:
    """Render current factory state via FBSR/Redis, return relative path or None."""
    from fbsr_bridge.fbsr_client import export_blueprint, enqueue_render, wait_for_render
    rel_path = f"screenshots/{session_id}/step_{step:04d}.png"
    full_path = os.path.join(SCREENSHOT_DIR, rel_path)
    try:
        bp = export_blueprint(rcon)
        if not bp.startswith("0"):
            return None
        jid = enqueue_render(bp, full_path, job_id=f"{session_id}_{step:04d}")
        status = wait_for_render(jid, timeout=15.0)
        return rel_path if (status == "OK" and os.path.exists(full_path)) else None
    except Exception as e:
        print(f"⚠️  screenshot failed: {e}")
        return None


def extract_code(text: str) -> str:
    """Pull the first ```python ... ``` block, or fall back to whole text."""
    if "```python" in text:
        after = text.split("```python", 1)[1]
        return after.split("```", 1)[0].strip()
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 3:
            return parts[1].lstrip("python\n").strip()
    return text.strip()


def call_llm(prompt: str) -> tuple[str, int, int]:
    if MODEL == "scripted":
        # Canned test script for smoke-testing the REPL
        text = """```python
ore = world.nearest('iron-ore')
print(f"ore at {ore}")
if ore:
    drill = world.place('burner-mining-drill', ore, 'south')
    print(f"drill={drill}")
    if drill:
        furnace = world.place_next_to('stone-furnace', drill, 'south')
        print(f"furnace={furnace}")
        world.insert(drill, 'coal', 50)
        world.insert(furnace, 'coal', 50)
        print(f"score={world.score()}")
```"""
        return text, len(prompt) // 4, len(text) // 4
    if MODEL.startswith("cc/"):
        cc_model = MODEL[3:]
        try:
            res = subprocess.run(
                ["claude", "-p", "--model", cc_model],
                input=prompt, capture_output=True, text=True, timeout=120,
            )
            text = res.stdout.strip()
        except subprocess.TimeoutExpired:
            text = ""
        return text, len(prompt) // 4, len(text) // 4
    response = litellm.completion(
        model=MODEL, max_tokens=1024,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content,
            response.usage.prompt_tokens, response.usage.completion_tokens)


def exec_code(code: str, world: World) -> tuple[str, str | None]:
    """Run code in a fresh namespace with world/Pos/Ent exposed. Returns (stdout, err)."""
    buf = io.StringIO()
    ns = {"world": world, "Pos": Pos, "Ent": Ent, "__builtins__": __builtins__}
    err = None
    try:
        with redirect_stdout(buf):
            exec(compile(code, "<agent>", "exec"), ns)
    except Exception:
        err = traceback.format_exc(limit=5)
    return buf.getvalue(), err


def observe(world: World) -> str:
    ents = world.get_entities(within=80, origin=Pos(0, 0))
    # also scan around iron ore region (agents tend to build far from origin)
    seen = {(e.unit_number): e for e in ents}
    ore = world.nearest("iron-ore")
    if ore:
        for e in world.get_entities(within=40, origin=ore):
            seen[e.unit_number] = e
    lines = [f"Score: {world.score():.0f}", f"Entities ({len(seen)}):"]
    for e in list(seen.values())[:20]:
        inv = world.inventory(e) if e.name.endswith(("drill", "furnace", "chest")) else {}
        inv_s = (" " + ",".join(f"{k}:{v}" for k, v in inv.items())) if inv else ""
        lines.append(f"  {e!r} un={e.unit_number}{inv_s}")
    if ore:
        lines.append(f"Nearest iron-ore: {ore}")
    return "\n".join(lines)


def main():
    session_id = f"agent_{uuid.uuid4().hex[:8]}"
    print(f"🤖 Agent v2: session={session_id} model={MODEL} steps={MAX_STEPS}")
    print(f"🔗 Weave: https://wandb.ai/{WEAVE_ENTITY}/{WEAVE_PROJECT}/weave/traces")

    rcon = RCONClient(RCON_HOST, RCON_PORT, RCON_PASS)
    world = World(rcon)
    pipe = ensure_pipe()

    # Ack achievement warning + unlock everything
    rcon.send_command("/sc game.tick")
    world._lua(
        'local f=game.forces["player"] '
        'for _,t in pairs(f.technologies) do t.researched=true end '
        'for _,r in pairs(f.recipes) do r.enabled=true end'
    )
    print("🔓 Unlocked all tech + recipes")

    write_event(pipe, {"type": "session_init", "session_id": session_id,
                       "tick": 0, "level_name": "agent"})
    write_event(pipe, {"type": "agent", "event_name": "trajectory_start",
                       "session_id": session_id, "model": MODEL,
                       "task": TASK, "max_steps": MAX_STEPS})

    last_stdout = ""
    last_err = None
    total_tokens, total_latency, error_steps = 0, 0, 0
    final_score = 0.0

    for step in range(1, MAX_STEPS + 1):
        print(f"\n── Step {step}/{MAX_STEPS} ──")
        obs = observe(world)
        print(obs)

        feedback = ""
        if last_stdout:
            feedback += f"\nStdout from last turn:\n{last_stdout.rstrip()}\n"
        if last_err:
            feedback += f"\nException from last turn:\n{last_err.rstrip()}\n"

        prompt = f"{SYSTEM_PROMPT}\n\n## Current state\n{obs}{feedback}\n\n## Your turn (one ```python block)"
        write_event(pipe, {"type": "agent", "event_name": "step_start",
                           "session_id": session_id, "step": step,
                           "observation": obs})

        t0 = time.time()
        llm_text, tok_in, tok_out = call_llm(prompt)
        latency_ms = int((time.time() - t0) * 1000)
        total_tokens += tok_in + tok_out
        total_latency += latency_ms

        code = extract_code(llm_text)
        print(f"LLM ({latency_ms}ms), {len(code)} chars of code")
        if not code:
            error_steps += 1
            last_stdout, last_err = "", "(empty LLM response)"
            continue

        write_event(pipe, {"type": "agent", "event_name": "llm_response",
                           "session_id": session_id, "step": step,
                           "code": code, "reasoning": llm_text,
                           "tokens_in": tok_in, "tokens_out": tok_out,
                           "latency_ms": latency_ms})

        last_stdout, last_err = exec_code(code, world)
        if last_err:
            error_steps += 1
            print(f"  ERR: {last_err.splitlines()[-1] if last_err else ''}")
        if last_stdout:
            print(f"  stdout: {last_stdout.strip()[:200]}")

        final_score = world.score()

        write_event(pipe, {"type": "agent", "event_name": "code_result",
                           "session_id": session_id, "step": step,
                           "code": code, "output": last_stdout,
                           "error": last_err, "reward": 0,
                           "production_score": final_score})

        ss = take_screenshot(rcon, session_id, step)
        if ss:
            write_event(pipe, {"type": "agent", "event_name": "screenshot",
                               "session_id": session_id, "step": step,
                               "screenshot_path": ss})

    write_event(pipe, {"type": "agent", "event_name": "trajectory_end",
                       "session_id": session_id, "total_steps": MAX_STEPS,
                       "final_score": final_score, "reason": "max_steps"})
    if pipe:
        pipe.close()

    avg_lat = total_latency / MAX_STEPS if MAX_STEPS > 0 else 0
    save_result(session_id, final_score, MAX_STEPS, total_tokens, avg_lat, error_steps)
    print(f"\n🏁 Done. Final score: {final_score}")
    print(f"🔗 Trace: https://wandb.ai/{WEAVE_ENTITY}/{WEAVE_PROJECT}/weave/traces?filter=\"{session_id}\"")


RESULTS_PATH = os.path.join(os.path.dirname(__file__), "docs", "results.json")


def save_result(session_id, final_score, total_steps, total_tokens, avg_latency_ms, error_steps):
    try:
        with open(RESULTS_PATH) as f:
            results = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        results = []
    results.append({
        "session_id": session_id, "model": MODEL, "task": TASK,
        "final_score": final_score, "total_steps": total_steps,
        "total_tokens": total_tokens, "avg_latency_ms": avg_latency_ms,
        "error_steps": error_steps,
        "agent_version": "v2-python-repl",
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    })
    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
