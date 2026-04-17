#!/usr/bin/env python3
"""
Factorio agent loop — action-based, real player control.
The agent controls the player character: walk, mine, craft, place.
No cheating via create_entity — everything goes through player inventory.

Pipeline: agent_loop.py → named pipe → Rust client → Weave traces → wandb.ai
"""

import datetime
import json
import math
import os
import re
import subprocess
import time
import uuid

import litellm
from factorio_rcon import RCONClient

from player_actions import (
    get_player_state, execute_action, scan_area, ACTIONS
)

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
MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-4-20250514")
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "100"))
TASK = os.environ.get("AGENT_TASK", "open_play")
SCREENSHOT_DIR = os.environ.get(
    "FACTORIO_OUTPUT_PATH",
    os.path.expanduser("~/Library/Application Support/factorio/script-output"),
)

ACTION_LIST = "\n".join(f"  {desc}" for _, desc in ACTIONS.values())

SYSTEM_PROMPT = f"""\
You are a Factorio agent running in CHEAT MODE — all tech is unlocked and you
spawn entities directly on the surface via RCON. No walking, no mining, no
inventory. Your job: build a working factory that produces science packs.

Each turn you see the current state (entities near origin, production stats).
Respond with ONE action per turn. Just the action text, nothing else.

Available actions:
{ACTION_LIST}

STRATEGY — build a working iron-plate → science-pack chain near (0,0):
1. scan 100 — find nearby ore patches (iron-ore, copper-ore, coal, stone)
2. create_entity burner-mining-drill <x> <y> south — spawn drill on iron-ore
3. create_entity stone-furnace <x> <y+2> — spawn furnace below drill output
4. create_entity burner-inserter <x+1> <y+2> west — insert from furnace to chest
5. create_entity wooden-chest <x+2> <y+2> — collect iron plates
6. lua game.surfaces[1].find_entities_filtered{{name="stone-furnace"}}[1].insert{{name="coal",count=50}} — fuel the furnace
7. lua game.surfaces[1].find_entities_filtered{{name="burner-mining-drill"}}[1].insert{{name="coal",count=50}} — fuel the drill
8. create_entity assembling-machine-1 <x> <y> — craft science packs
9. list_entities 50 — verify what you built

RULES:
- ONE action per turn, just the action text
- No markdown, no explanation, no code blocks
- Prefer create_entity over craft/place (you have no inventory in cheat mode)
- Use `lua` for anything not covered by actions
- Drills need to be placed ON ore tiles, facing the direction of output
- Burner machines need coal fuel — insert via `lua`
"""


# ── Helpers ─────────────────────────────────────────────────────────
def write_event(pipe, event: dict):
    """Write a JSON event line to the pipe."""
    if pipe:
        pipe.write(json.dumps(event, ensure_ascii=False) + "\n")
        pipe.flush()


def ensure_pipe():
    """Open the named pipe non-blocking. Returns None if no Rust client is reading.
    Blocking open would hang forever until a reader attaches."""
    print(f"🔌 Opening pipe: {PIPE_PATH}")
    try:
        fd = os.open(PIPE_PATH, os.O_WRONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        print("⚠️  Pipe missing — run `mkfifo` first. Continuing without tracing.")
        return None
    except OSError as e:
        # errno 6 (ENXIO) = no reader on the FIFO
        print(f"⚠️  No pipe reader ({e}). Continuing without Weave tracing.")
        return None
    pipe = os.fdopen(fd, "w")
    print("✅ Pipe connected")
    return pipe


def take_screenshot(rcon: RCONClient, session_id: str, step: int) -> str | None:
    """Render a top-down PNG of the play area via FBSR worker (Redis queue).
    Headless — no Factorio client needed. Blocks up to ~15s waiting for render."""
    from fbsr_bridge.fbsr_client import export_blueprint, enqueue_render, wait_for_render

    rel_path = f"screenshots/{session_id}/step_{step:04d}.png"
    full_path = os.path.join(SCREENSHOT_DIR, rel_path)
    try:
        bp = export_blueprint(rcon, area=((-40, -40), (40, 40)))
        if not bp or not bp.startswith("0"):
            return None
        jid = enqueue_render(bp, full_path, job_id=f"{session_id}_{step:04d}")
        status = wait_for_render(jid, timeout=15.0)
        if status == "OK" and os.path.exists(full_path):
            return rel_path
        return None
    except Exception as e:
        print(f"⚠️  render failed: {e}")
        return None


def get_production_score(rcon: RCONClient) -> float:
    """Sum of all items produced."""
    try:
        result = rcon.send_command(
            "/sc local s=game.forces['player'].get_item_production_statistics('nauvis') "
            "local total=0 "
            "for _,count in pairs(s.input_counts) do total=total+count end "
            "rcon.print(total)"
        )
        return float(result.strip())
    except Exception:
        return 0.0


def parse_action_from_llm(text: str) -> str:
    """Extract action from LLM response. Strips markdown/explanations."""
    if not text:
        return ""
    # Try to find a line that starts with a known action
    action_names = list(ACTIONS.keys())
    for line in text.strip().split("\n"):
        line = line.strip().strip("`").strip()
        if any(line.lower().startswith(a) for a in action_names):
            return line
    # Fallback: return first non-empty line
    for line in text.strip().split("\n"):
        line = line.strip().strip("`").strip()
        if line and not line.startswith("#") and not line.startswith("*"):
            return line
    return text.strip()


_SCRIPTED_ACTIONS = [
    "unlock_all",
    "create_entity stone-furnace 0 0",
    "create_entity stone-furnace 2 0",
    "create_entity burner-mining-drill 5 0 south",
    "create_entity wooden-chest 8 0",
    "create_entity assembling-machine-1 -4 0",
    "create_entity assembling-machine-2 -7 0",
    "list_entities 30",
    'lua game.surfaces[1].find_entities_filtered{name="stone-furnace"}[1].insert{name="coal",count=50} rcon.print("fueled")',
    "list_entities 30",
]
_SCRIPTED_IDX = [0]


def call_llm(prompt: str) -> tuple[str, int, int]:
    """Call LLM and return (text, tokens_in, tokens_out)."""
    if MODEL == "scripted":
        # Smoke-test mode: cycle through a canned action sequence.
        text = _SCRIPTED_ACTIONS[_SCRIPTED_IDX[0] % len(_SCRIPTED_ACTIONS)]
        _SCRIPTED_IDX[0] += 1
        return text, len(prompt) // 4, len(text) // 4
    if MODEL.startswith("cc/"):
        cc_model = MODEL[3:]
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", cc_model],
                input=prompt, capture_output=True, text=True, timeout=120,
            )
            text = result.stdout.strip()
        except subprocess.TimeoutExpired:
            text = ""
        return text, len(prompt) // 4, len(text) // 4
    else:
        response = litellm.completion(
            model=MODEL,
            max_tokens=256,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        text = response.choices[0].message.content
        return text, response.usage.prompt_tokens, response.usage.completion_tokens


# ── Main loop ───────────────────────────────────────────────────────
def main():
    session_id = f"agent_{uuid.uuid4().hex[:8]}"
    print(f"🤖 Agent: session={session_id} model={MODEL} steps={MAX_STEPS}")

    rcon = RCONClient(RCON_HOST, RCON_PORT, RCON_PASS)
    pipe = ensure_pipe()

    # Clear achievement warning
    rcon.send_command("/sc game.tick")
    time.sleep(0.1)
    rcon.send_command("/sc game.tick")

    # Report player connection state (headless OK — cheat mode bypasses player)
    r = rcon.send_command('/sc rcon.print(#game.connected_players)')
    headless = (r and r.strip() == "0")
    print(f"{'🎮 Headless mode (no client)' if headless else '✅ Player connected'}")

    # Bootstrap cheat mode: unlock all tech + recipes so the agent can build anything
    from player_actions import unlock_all
    print(f"🔓 {unlock_all(rcon)}")

    write_event(pipe, {
        "type": "session_init",
        "session_id": session_id,
        "tick": 0,
        "level_name": "agent",
    })
    write_event(pipe, {
        "type": "agent",
        "event_name": "trajectory_start",
        "session_id": session_id,
        "model": MODEL,
        "task": TASK,
        "max_steps": MAX_STEPS,
    })

    total_tokens = 0
    total_latency = 0
    error_steps = 0
    final_score = 0.0
    last_actions = []  # rolling history for context

    for step in range(1, MAX_STEPS + 1):
        print(f"\n── Step {step}/{MAX_STEPS} ──")

        # Observe
        try:
            state = get_player_state(rcon)
        except Exception:
            print("🔄 RCON reconnecting...")
            rcon = RCONClient(RCON_HOST, RCON_PORT, RCON_PASS)
            state = get_player_state(rcon)

        obs = (
            f"Position: ({state['position']['x']:.0f}, {state['position']['y']:.0f})\n"
            f"Inventory: {state['inventory']}\n"
            f"Nearby resources: {state['nearby_resources']}\n"
            f"Nearby buildings: {state['nearby_buildings']}\n"
            f"Research: {state['research']}\n"
            f"Tick: {state['tick']}"
        ) if state.get("position") else "ERROR: no player connected"

        print(obs)

        write_event(pipe, {
            "type": "agent",
            "event_name": "step_start",
            "session_id": session_id,
            "step": step,
            "observation": obs,
        })

        # Build prompt — system + observation + recent action history
        history = ""
        if last_actions:
            history = "\nRecent actions:\n" + "\n".join(
                f"  {a['action']} → {a['result'][:100]}" for a in last_actions[-5:]
            ) + "\n"

        prompt = f"{SYSTEM_PROMPT}\n\nCurrent state:\n{obs}{history}\nYour action:"

        # LLM call
        t0 = time.time()
        llm_text, tokens_in, tokens_out = call_llm(prompt)
        latency_ms = int((time.time() - t0) * 1000)
        total_tokens += tokens_in + tokens_out
        total_latency += latency_ms

        action_text = parse_action_from_llm(llm_text)
        print(f"LLM ({latency_ms}ms): {action_text or '(empty)'}")
        if not action_text and llm_text:
            print(f"  RAW: {llm_text[:200]}")

        write_event(pipe, {
            "type": "agent",
            "event_name": "llm_response",
            "session_id": session_id,
            "step": step,
            "code": action_text,
            "reasoning": llm_text,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
        })

        # Execute action
        if action_text:
            try:
                result = execute_action(rcon, action_text)
            except Exception as e:
                result = f"ERROR: {e}"
                error_steps += 1
        else:
            result = "(no action)"
            error_steps += 1

        print(f"Result: {result[:200]}")

        last_actions.append({"action": action_text, "result": result})

        final_score = get_production_score(rcon)

        write_event(pipe, {
            "type": "agent",
            "event_name": "code_result",
            "session_id": session_id,
            "step": step,
            "code": action_text,
            "output": result,
            "error": None if "ERROR" not in result else result,
            "reward": 0,
            "production_score": final_score,
        })

        # Screenshot
        ss_path = take_screenshot(rcon, session_id, step)
        if ss_path:
            write_event(pipe, {
                "type": "agent",
                "event_name": "screenshot",
                "session_id": session_id,
                "step": step,
                "screenshot_path": ss_path,
            })

    # End
    write_event(pipe, {
        "type": "agent",
        "event_name": "trajectory_end",
        "session_id": session_id,
        "total_steps": MAX_STEPS,
        "final_score": final_score,
        "reason": "max_steps",
    })

    if pipe:
        pipe.close()

    avg_lat = total_latency / MAX_STEPS if MAX_STEPS > 0 else 0
    save_result(session_id, final_score, MAX_STEPS, total_tokens, avg_lat, error_steps)
    print(f"\n🏁 Done. Final score: {final_score}")


RESULTS_PATH = os.path.join(os.path.dirname(__file__), "docs", "results.json")


def save_result(session_id, final_score, total_steps, total_tokens, avg_latency_ms, error_steps):
    """Append run result to docs/results.json."""
    try:
        with open(RESULTS_PATH, "r") as f:
            results = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        results = []

    results.append({
        "session_id": session_id,
        "model": MODEL,
        "task": TASK,
        "final_score": final_score,
        "total_steps": total_steps,
        "total_tokens": total_tokens,
        "avg_latency_ms": avg_latency_ms,
        "error_steps": error_steps,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    })

    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)

    print(f"📊 Result saved to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
