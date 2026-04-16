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

import subprocess

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
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "100"))
TASK = os.environ.get("AGENT_TASK", "open_play")
SCREENSHOT_DIR = os.environ.get(
    "FACTORIO_OUTPUT_PATH",
    os.path.expanduser("~/Library/Application Support/factorio/script-output"),
)

SYSTEM_PROMPT = """\
You are a Factorio agent controlling a headless server via RCON Lua commands.

Each turn you receive: tick, production stats, research status, entity count.
Respond with:
1. A short PLAN (what you want to do and why)
2. A ```lua code block with commands to execute

HEADLESS MODE SPECIFICS:
- No player character exists - use force="player" for entities
- Technologies start locked - unlock them with force.technologies["..."].researched = true
- Recipes start disabled - enable them with force.recipes["..."].enabled = true
- Always use find_entities_filtered{force="player"} NOT find_entities() (avoids listing trees/rocks)
- Entity placement requires valid terrain and unlocked technologies

Key APIs (headless, no game.player):
- game.surfaces[1] — the main surface (nauvis)
- game.forces["player"] — the player force
- game.surfaces[1].create_entity{name="...", position={x,y}, force="player"} — place entities
- game.surfaces[1].find_entities_filtered{force="player"} — find YOUR entities only
- game.forces["player"].technologies["..."].researched = true — unlock tech
- game.forces["player"].recipes["..."].enabled = true — enable recipes
- rcon.print(...) — output results you need to see

BOOTSTRAP SEQUENCE (first few turns):
1. Unlock basic technologies: automation, electronics, steel-processing
2. Enable basic recipes: iron-plate, copper-plate, stone-furnace, burner-mining-drill, etc.
3. Find resources with find_entities_filtered{name="iron-ore", area={{-100,-100},{100,100}}}
4. Place mining drills on resource patches
5. Connect with smelting: drill → inserter → furnace → inserter → chest

PRODUCTION STRATEGY:
- Build incrementally: mine → smelt → assemble → science packs
- Feed assemblers manually with .insert{} commands to keep production flowing
- Scale with multiple assemblers for higher throughput
- Aim for automation-science-pack production first, then logistic-science-pack

Rules:
- Use rcon.print() to output important results
- Keep each turn focused on one clear goal
- Always check what exists before building with find_entities_filtered{force="player"}
- Resources may be far from spawn (100+ tiles away) — search large areas
- If you can't find iron/copper nearby, expand search to area={{-200,-200},{200,200}} or wider
- Unlock technologies and enable recipes BEFORE trying to place entities
- Use manual material feeding (.insert{}) to bootstrap production chains
"""


# ── Helpers ─────────────────────────────────────────────────────────
def write_event(pipe, event: dict):
    """Write a JSON event line to the pipe."""
    if pipe:
        pipe.write(json.dumps(event, ensure_ascii=False) + "\n")
        pipe.flush()


def get_observation(rcon: RCONClient) -> str:
    """Pull game state from Factorio via RCON (headless compatible)."""
    parts = []

    # Game tick
    try:
        tick = rcon.send_command("/sc rcon.print(game.tick)")
        parts.append(f"Tick: {tick.strip()}")
    except Exception as e:
        parts.append(f"Tick: error ({e})")

    # Player (if connected) or note headless
    try:
        player_check = rcon.send_command(
            '/sc local p=game.connected_players[1]; '
            'if p then rcon.print(string.format("x=%.1f y=%.1f", p.position.x, p.position.y)) '
            'else rcon.print("no_player") end'
        )
        pos = player_check.strip()
        if pos == "no_player":
            parts.append("Mode: headless (no player connected)")
        else:
            parts.append(f"Position: {pos}")
    except Exception as e:
        parts.append(f"Position: error ({e})")

    # Production stats (force-level, works headless)
    try:
        prod = rcon.send_command(
            "/sc local f=game.forces['player'] "
            "local s=f.get_item_production_statistics('nauvis') "
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
            "/sc local r=game.forces['player'].current_research "
            "if r then rcon.print(r.name..' '..string.format('%.0f%%', r.research_progress*100)) "
            "else rcon.print('(none)') end"
        )
        parts.append(f"Research: {research.strip()}")
    except Exception as e:
        parts.append(f"Research: error ({e})")

    # Entity count on surface
    try:
        entities = rcon.send_command(
            "/sc rcon.print(#game.surfaces[1].find_entities_filtered{force='player'})"
        )
        parts.append(f"Entities: {entities.strip()}")
    except Exception as e:
        parts.append(f"Entities: error ({e})")

    return "\n".join(parts)


def get_production_score(rcon: RCONClient) -> float:
    """Simple production score: sum of all items produced."""
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


def bootstrap_headless_session(rcon: RCONClient) -> bool:
    """Bootstrap headless session: clear achievement warning and unlock basic technologies."""
    try:
        print("🚀 Bootstrapping headless session...")

        # Clear achievement warning by sending dummy command twice
        print("  Clearing achievement warning...")
        rcon.send_command("/sc game.tick")  # First call triggers warning
        time.sleep(0.1)
        rcon.send_command("/sc game.tick")  # Second call actually executes

        # Unlock essential starting technologies
        print("  Unlocking basic technologies...")
        tech_commands = [
            "local force = game.forces['player']",
            "force.technologies['automation'].researched = true",
            "force.technologies['electronics'].researched = true",
            "force.technologies['steel-processing'].researched = true",
            "force.technologies['logistics'].researched = true",
        ]
        rcon.send_command("/sc " + "; ".join(tech_commands))

        # Enable essential recipes
        print("  Enabling basic recipes...")
        recipe_commands = [
            "force.recipes['iron-plate'].enabled = true",
            "force.recipes['copper-plate'].enabled = true",
            "force.recipes['stone-furnace'].enabled = true",
            "force.recipes['burner-mining-drill'].enabled = true",
            "force.recipes['wooden-chest'].enabled = true",
            "force.recipes['transport-belt'].enabled = true",
            "force.recipes['inserter'].enabled = true",
            "force.recipes['lab'].enabled = true",
            "force.recipes['assembling-machine-1'].enabled = true",
            "force.recipes['electronic-circuit'].enabled = true",
            "force.recipes['copper-cable'].enabled = true",
            "force.recipes['iron-gear-wheel'].enabled = true",
            "force.recipes['automation-science-pack'].enabled = true",
            "force.recipes['logistic-science-pack'].enabled = true"
        ]
        rcon.send_command("/sc " + "; ".join(recipe_commands))

        # Verify bootstrap success
        result = rcon.send_command("/sc rcon.print('Bootstrap complete - ' .. tostring(game.forces['player'].technologies['automation'].researched))")
        if "Bootstrap complete - true" in result:
            print("✅ Bootstrap successful!")
            return True
        else:
            print(f"⚠️  Bootstrap verification failed: {result}")
            return False

    except Exception as e:
        print(f"❌ Bootstrap failed: {e}")
        return False


def weave_sender_running() -> bool:
    """Check if weave-sender process is running."""
    try:
        result = subprocess.run(["pgrep", "-f", "weave-sender"],
                              capture_output=True, text=True)
        return result.returncode == 0
    except:
        return False


def ensure_weave_client():
    """Start weave-sender if not running, with fallback to no-pipe mode."""
    try:
        # Check if weave-sender is already running
        if not weave_sender_running():
            print("🔄 Starting weave-sender client...")
            # Start weave-sender in background
            weave_process = subprocess.Popen(
                ["./binaries/weave-sender"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            time.sleep(2)  # Brief startup delay

            if weave_sender_running():
                print("✅ weave-sender started successfully")
            else:
                print("⚠️  weave-sender startup failed, continuing without tracing")
                return None
        else:
            print("✅ weave-sender already running")

        # TEMP: Skip pipe connection until weave integration is properly configured
        # The weave-sender uses socket files, not this named pipe
        print("⚠️  Skipping pipe connection (weave uses sockets, not pipe)")
        print("📝 Agent will run without Weave tracing for now")
        return None

    except Exception as e:
        print(f"⚠️  Pipe connection failed ({e}), continuing without tracing")
        return None


def take_screenshot(rcon: RCONClient, session_id: str, step: int) -> str | None:
    """Take a screenshot via RCON, copy from Docker, return the local path."""
    rel_path = f"screenshots/{session_id}/step_{step:04d}.png"
    local_dir = os.path.join(SCREENSHOT_DIR, "screenshots", session_id)
    local_path = os.path.join(local_dir, f"step_{step:04d}.png")
    try:
        rcon.send_command(
            f'/sc game.take_screenshot{{surface=game.surfaces[1], '
            f'position={{0,0}}, resolution={{1920,1080}}, zoom=0.3, '
            f'path="{rel_path}", show_entity_info=true}}'
        )
        time.sleep(0.3)  # wait for file to be written
        os.makedirs(local_dir, exist_ok=True)
        subprocess.run(
            ["docker", "cp", f"factorio:/factorio/script-output/{rel_path}", local_path],
            capture_output=True, timeout=10,
        )
        if os.path.exists(local_path):
            return rel_path
        print(f"⚠️  docker cp failed for {rel_path}")
        return None
    except Exception as e:
        print(f"⚠️  Screenshot failed: {e}")
        return None


# ── Main loop ───────────────────────────────────────────────────────
def main():
    session_id = f"agent_{uuid.uuid4().hex[:8]}"

    print(f"🤖 Starting agent: session={session_id} model={MODEL} task={TASK}")
    print(f"   RCON: {RCON_HOST}:{RCON_PORT}")
    print(f"   Pipe: {PIPE_PATH}")

    def connect_rcon():
        return RCONClient(RCON_HOST, RCON_PORT, RCON_PASS)

    rcon = connect_rcon()
    pipe = ensure_weave_client()  # Auto-start weave-sender + connect to pipe

    # Bootstrap headless session (clear achievement warning + unlock technologies)
    if not bootstrap_headless_session(rcon):
        print("❌ Failed to bootstrap session, continuing anyway...")

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

        # Observe (reconnect RCON if needed)
        try:
            obs = get_observation(rcon)
        except Exception:
            print("🔄 RCON reconnecting...")
            rcon = connect_rcon()
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

        # LLM call
        messages.append({"role": "user", "content": obs})
        t0 = time.time()

        if MODEL.startswith("cc/"):
            # Claude Code backend: uses Max subscription, no API key needed
            cc_model = MODEL[3:]  # strip "cc/" prefix
            prompt = SYSTEM_PROMPT + "\n\n" + "\n\n".join(
                f"{'[User]' if m['role']=='user' else '[Assistant]'}: {m['content']}"
                for m in messages
            )
            try:
                result_proc = subprocess.run(
                    ["claude", "-p", "--model", cc_model],
                    input=prompt, capture_output=True, text=True, timeout=300,
                )
                llm_text = result_proc.stdout.strip()
            except subprocess.TimeoutExpired:
                print(f"⚠️  claude -p timed out at 300s, skipping step {step}")
                llm_text = ""
            tokens_in = len(prompt) // 4  # estimate
            tokens_out = len(llm_text) // 4
        else:
            # litellm: works with any model/provider (needs API key)
            response = litellm.completion(
                model=MODEL,
                max_tokens=2048,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages,
            )
            llm_text = response.choices[0].message.content
            tokens_in = response.usage.prompt_tokens
            tokens_out = response.usage.completion_tokens

        latency_ms = int((time.time() - t0) * 1000)
        code = parse_lua_code(llm_text)
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
