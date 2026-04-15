#!/usr/bin/env python3
"""
Minimal Factorio agent loop.
  - Connects to Factorio via RCON
  - Calls LLM to generate Lua commands
  - Executes via RCON, reads result
  - Writes structured events to named pipe → Rust client → Weave traces → wandb.ai
"""

import datetime
import json
import os
import re
import time
import uuid

import litellm
from factorio_rcon import RCONClient

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
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "50"))
TASK = os.environ.get("AGENT_TASK", "open_play")

SYSTEM_PROMPT = """\
You are a Factorio agent. You control the game by writing Lua commands that run via RCON.

Each turn you receive the current game state (inventory, production, position).
Respond with:
1. A short PLAN (what you want to do and why)
2. A ```lua code block with commands to execute

Rules:
- Use /sc prefix convention: your code runs as `game.player.` context
- Use rcon.print() to output results you need
- Keep each turn focused on one clear goal
- Build incrementally: gather → craft → automate
- Check what you have before trying to build
"""


# ── Helpers ─────────────────────────────────────────────────────────
def write_event(pipe, event: dict):
    """Write a JSON event line to the pipe."""
    pipe.write(json.dumps(event, ensure_ascii=False) + "\n")
    pipe.flush()


def get_observation(rcon: RCONClient) -> str:
    """Pull game state from Factorio via RCON."""
    parts = []

    # Player position
    try:
        pos = rcon.send_command(
            '/sc local p=game.player.position; rcon.print(string.format("x=%.1f y=%.1f", p.x, p.y))'
        )
        parts.append(f"Position: {pos.strip()}")
    except Exception as e:
        parts.append(f"Position: error ({e})")

    # Inventory
    try:
        inv = rcon.send_command(
            "/sc local inv=game.player.get_main_inventory() "
            "local t={} "
            "for name,count in pairs(inv.get_contents()) do "
            "  t[#t+1]=name..':'..count "
            "end "
            "rcon.print(table.concat(t, ', '))"
        )
        parts.append(f"Inventory: {inv.strip() or '(empty)'}")
    except Exception as e:
        parts.append(f"Inventory: error ({e})")

    # Production stats (top items)
    try:
        prod = rcon.send_command(
            "/sc local s=game.player.force.get_item_production_statistics('nauvis') "
            "local t={} "
            "for name,_ in pairs(s.input_counts) do "
            "  local r=s.get_flow_count{name=name,input=true,precision_index=defines.flow_precision_index.one_minute,count=false} "
            "  if r>0 then t[#t+1]=name..':'..string.format('%.1f',r)..'/m' end "
            "end "
            "rcon.print(table.concat(t, ', '))"
        )
        parts.append(f"Production: {prod.strip() or '(none)'}")
    except Exception as e:
        parts.append(f"Production: error ({e})")

    # Research
    try:
        research = rcon.send_command(
            "/sc local r=game.player.force.current_research "
            "if r then rcon.print(r.name..' '..string.format('%.0f%%', r.research_progress*100)) "
            "else rcon.print('(none)') end"
        )
        parts.append(f"Research: {research.strip()}")
    except Exception as e:
        parts.append(f"Research: error ({e})")

    return "\n".join(parts)


def get_production_score(rcon: RCONClient) -> float:
    """Simple production score: sum of all items produced."""
    try:
        result = rcon.send_command(
            "/sc local s=game.player.force.get_item_production_statistics('nauvis') "
            "local total=0 "
            "for _,count in pairs(s.input_counts) do total=total+count end "
            "rcon.print(total)"
        )
        return float(result.strip())
    except Exception:
        return 0.0


def parse_lua_code(text: str) -> str | None:
    """Extract ```lua code block from LLM response."""
    m = re.search(r"```lua\s*\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else None


def execute_code(rcon: RCONClient, code: str) -> dict:
    """Execute Lua code via RCON, return output/error."""
    try:
        result = rcon.send_command(f"/sc {code}")
        return {"output": result.strip(), "error": None}
    except Exception as e:
        return {"output": "", "error": str(e)}


# ── Main loop ───────────────────────────────────────────────────────
def main():
    session_id = f"agent_{uuid.uuid4().hex[:8]}"

    print(f"🤖 Starting agent: session={session_id} model={MODEL} task={TASK}")
    print(f"   RCON: {RCON_HOST}:{RCON_PORT}")
    print(f"   Pipe: {PIPE_PATH}")

    rcon = RCONClient(RCON_HOST, RCON_PORT, RCON_PASS)
    pipe = open(PIPE_PATH, "a")

    # Session init (triggers Rust client session creation)
    write_event(
        pipe,
        {
            "type": "session_init",
            "session_id": session_id,
            "tick": 0,
            "level_name": "agent",
        },
    )

    # Trajectory start
    write_event(
        pipe,
        {
            "type": "agent",
            "event_name": "trajectory_start",
            "session_id": session_id,
            "model": MODEL,
            "task": TASK,
            "max_steps": MAX_STEPS,
        },
    )

    messages = []
    final_score = 0.0
    total_tokens = 0
    total_latency = 0
    error_steps = 0

    for step in range(1, MAX_STEPS + 1):
        print(f"\n── Step {step}/{MAX_STEPS} ──")

        # Observe
        obs = get_observation(rcon)
        print(obs)

        write_event(
            pipe,
            {
                "type": "agent",
                "event_name": "step_start",
                "session_id": session_id,
                "step": step,
                "observation": obs,
            },
        )

        # LLM call (litellm: works with any model/provider)
        messages.append({"role": "user", "content": obs})
        t0 = time.time()
        response = litellm.completion(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        )
        latency_ms = int((time.time() - t0) * 1000)

        llm_text = response.choices[0].message.content
        code = parse_lua_code(llm_text)
        tokens_in = response.usage.prompt_tokens
        tokens_out = response.usage.completion_tokens
        total_tokens += tokens_in + tokens_out
        total_latency += latency_ms
        messages.append({"role": "assistant", "content": llm_text})

        print(f"LLM ({latency_ms}ms, {tokens_out} tok): {code or '(no code)'}")

        write_event(
            pipe,
            {
                "type": "agent",
                "event_name": "llm_response",
                "session_id": session_id,
                "step": step,
                "code": code or "",
                "reasoning": llm_text,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "latency_ms": latency_ms,
            },
        )

        # Execute
        if code:
            result = execute_code(rcon, code)
        else:
            result = {"output": "(no code generated)", "error": None}

        final_score = get_production_score(rcon)

        if result["error"]:
            error_steps += 1
            print(f"ERROR: {result['error']}")
            messages.append(
                {"role": "user", "content": f"Error: {result['error']}"}
            )
        elif result["output"]:
            print(f"Result: {result['output'][:200]}")

        write_event(
            pipe,
            {
                "type": "agent",
                "event_name": "code_result",
                "session_id": session_id,
                "step": step,
                "code": code or "",
                "output": result["output"],
                "error": result["error"],
                "reward": 0,
                "production_score": final_score,
            },
        )

    # End trajectory
    write_event(
        pipe,
        {
            "type": "agent",
            "event_name": "trajectory_end",
            "session_id": session_id,
            "total_steps": MAX_STEPS,
            "final_score": final_score,
            "reason": "max_steps",
        },
    )

    pipe.close()

    # Append result to docs/results.json for the leaderboard
    avg_lat = total_latency / MAX_STEPS if MAX_STEPS > 0 else 0
    save_result(session_id, final_score, MAX_STEPS, total_tokens, avg_lat, error_steps)
    print(f"\n🏁 Done. Final score: {final_score}")


RESULTS_PATH = os.path.join(os.path.dirname(__file__), "docs", "results.json")


def save_result(session_id, final_score, total_steps, total_tokens, avg_latency_ms, error_steps):
    """Append run result to docs/results.json (static leaderboard data)."""
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
