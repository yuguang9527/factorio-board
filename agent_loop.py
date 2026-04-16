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
You are a Factorio agent controlling a real player character. You must play legitimately:
walk to resources, mine by hand, craft items, and place buildings from inventory.

Each turn you see your position, inventory, nearby resources, and buildings.
Respond with ONE action per turn. Just the action, nothing else.

Available actions:
{ACTION_LIST}

STRATEGY — follow this order:
1. scan 100 — find nearby resources
2. mine stone 10 — get stone for furnaces
3. mine coal 10 — get fuel
4. mine iron-ore 20 — get iron ore
5. craft stone-furnace 2 — make furnaces
6. place stone-furnace <x> <y> — place near resources
7. mine iron-ore 30 — mine more iron
8. lua <code> — feed furnace: game.connected_players[1].get_closest_entity({{name="stone-furnace"}}).insert({{name="iron-ore", count=20}})
9. craft burner-mining-drill 1 — automate mining
10. place burner-mining-drill <x> <y> south — place on iron ore patch

RULES:
- ONE action per turn, just the action text
- No markdown, no explanation, no code blocks
- You need ingredients to craft (check your inventory)
- Walk close before placing
- Furnaces need fuel (coal) AND ore to work
- Mine stone first (you start with nothing)
"""


# ── Helpers ─────────────────────────────────────────────────────────
def write_event(pipe, event: dict):
    """Write a JSON event line to the pipe."""
    if pipe:
        pipe.write(json.dumps(event, ensure_ascii=False) + "\n")
        pipe.flush()


def ensure_pipe():
    """Open the named pipe for writing. Rust client must already be reading."""
    try:
        print(f"🔌 Opening pipe: {PIPE_PATH}")
        pipe = open(PIPE_PATH, "w")
        print("✅ Pipe connected")
        return pipe
    except Exception as e:
        print(f"⚠️  Pipe failed ({e}), continuing without tracing")
        return None


def take_screenshot(rcon: RCONClient, session_id: str, step: int) -> str | None:
    """Take a screenshot centered on the player."""
    rel_path = f"screenshots/{session_id}/step_{step:04d}.png"
    full_path = os.path.join(SCREENSHOT_DIR, rel_path)
    try:
        rcon.send_command(
            f'/sc local p=game.connected_players[1] '
            f'if p then '
            f'  game.take_screenshot{{player=p, resolution={{1920,1080}}, zoom=0.5, '
            f'  path="{rel_path}", show_entity_info=true}} '
            f'else '
            f'  game.take_screenshot{{surface=game.surfaces[1], position={{0,0}}, '
            f'  resolution={{1920,1080}}, zoom=0.3, '
            f'  path="{rel_path}", show_entity_info=true}} '
            f'end'
        )
        time.sleep(0.3)
        if os.path.exists(full_path):
            return rel_path
        return None
    except Exception:
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


def call_llm(prompt: str) -> tuple[str, int, int]:
    """Call LLM and return (text, tokens_in, tokens_out)."""
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

    # Check player connected
    r = rcon.send_command('/sc rcon.print(#game.connected_players)')
    if r and r.strip() == "0":
        print("❌ No player connected! Start Factorio client and join the server.")
        return

    print("✅ Player connected")

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
