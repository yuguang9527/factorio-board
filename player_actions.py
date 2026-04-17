"""
Player action layer for Factorio agent.
Controls the player character via RCON — walk, mine, craft, place, etc.
No cheating: everything goes through player inventory and real game mechanics.
"""

import math
import time
from factorio_rcon import RCONClient


def _cmd(rcon: RCONClient, lua: str) -> str:
    """Send a /sc command and return the result."""
    return (rcon.send_command(f"/sc {lua}") or "").strip()


def get_player_state(rcon: RCONClient) -> dict:
    """Get full player state: position, inventory, nearby resources/entities.
    Falls back to origin-centered observation when no player is connected."""
    state = {}

    # Position — use player if connected, else origin
    r = _cmd(rcon,
        'local p=game.connected_players[1] '
        'if p then rcon.print(string.format("%.1f,%.1f", p.position.x, p.position.y)) '
        'else rcon.print("none") end')
    if r and r != "none":
        x, y = r.split(",")
        state["position"] = {"x": float(x), "y": float(y)}
        state["headless"] = False
    else:
        state["position"] = {"x": 0.0, "y": 0.0}
        state["headless"] = True

    px, py = state["position"]["x"], state["position"]["y"]

    # Inventory — player inventory if connected, else "(headless)"
    if state["headless"]:
        state["inventory"] = "(headless — no player inventory; use create_entity)"
    else:
        r = _cmd(rcon,
            'local p=game.connected_players[1] '
            'local items={"wood","stone","coal","iron-ore","copper-ore","iron-plate",'
            '"copper-plate","iron-gear-wheel","copper-cable","electronic-circuit",'
            '"stone-furnace","burner-mining-drill","burner-inserter","transport-belt",'
            '"wooden-chest","assembling-machine-1","automation-science-pack","inserter"} '
            'local t={} '
            'for _,name in pairs(items) do '
            '  local c=p.get_item_count(name) '
            '  if c>0 then t[#t+1]=name..":"..c end '
            'end '
            'rcon.print(#t>0 and table.concat(t,", ") or "(empty)")')
        state["inventory"] = r

    # Nearby resources (within 30 tiles)
    r = _cmd(rcon,
        f'local res={{}} '
        f'for _,name in pairs({{"iron-ore","copper-ore","coal","stone"}}) do '
        f'  local e=game.surfaces[1].find_entities_filtered{{name=name, '
        f'  position={{{px},{py}}}, radius=30, limit=1}} '
        f'  if #e>0 then '
        f'    local d=math.sqrt((e[1].position.x-{px})^2+(e[1].position.y-{py})^2) '
        f'    res[#res+1]=name..string.format(" %.0f tiles (%.0f,%.0f)", d, e[1].position.x, e[1].position.y) '
        f'  end '
        f'end '
        f'rcon.print(#res>0 and table.concat(res, "; ") or "(none within 30 tiles)")')
    state["nearby_resources"] = r

    # Nearby player entities
    r = _cmd(rcon,
        f'local ents=game.surfaces[1].find_entities_filtered{{force="player", '
        f'position={{{px},{py}}}, radius=30}} '
        f'local t={{}} '
        f'for _,e in pairs(ents) do '
        f'  if e.name~="character" then '
        f'    t[#t+1]=e.name..string.format(" (%.0f,%.0f)", e.position.x, e.position.y) '
        f'  end '
        f'end '
        f'rcon.print(#t>0 and table.concat(t, "; ") or "(none)")')
    state["nearby_buildings"] = r

    # Game tick
    state["tick"] = _cmd(rcon, 'rcon.print(game.tick)')

    # Research
    state["research"] = _cmd(rcon,
        'local r=game.forces["player"].current_research '
        'if r then rcon.print(r.name.." "..string.format("%.0f%%", r.research_progress*100)) '
        'else rcon.print("(none)") end')

    return state


_DIR_NAMES = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]


def _calc_direction(cx: float, cy: float, tx: float, ty: float) -> str:
    """Calculate Factorio direction name from current to target position.
    Factorio coords: +X=east, +Y=south. Returns defines.direction.xxx string."""
    dx, dy = tx - cx, ty - cy
    angle_deg = math.degrees(math.atan2(dy, dx))  # 0=east, 90=south(Factorio)
    factorio_angle = (angle_deg + 90) % 360  # rotate so 0=north
    idx = round(factorio_angle / 45) % 8
    return f"defines.direction.{_DIR_NAMES[idx]}"


def walk_to(rcon: RCONClient, x: float, y: float, timeout: float = 20.0) -> str:
    """Walk the player to a position. Each poll does: set direction + walk one step + check."""
    pos = _cmd(rcon,
        'local p=game.connected_players[1] '
        'rcon.print(p.position.x..","..p.position.y)')
    if not pos:
        return "ERROR: no player"
    cx, cy = [float(v) for v in pos.split(",")]
    dist = math.sqrt((x - cx)**2 + (y - cy)**2)

    if dist < 3:
        return f"Already at ({x:.0f},{y:.0f})"

    # Single Lua command: calculate direction, set walking, report position+distance
    WALK_CMD = (
        'local p=game.connected_players[1] '
        'local tx,ty=' + str(x) + ',' + str(y) + ' '
        'local dx,dy=tx-p.position.x,ty-p.position.y '
        'local dist=math.sqrt(dx*dx+dy*dy) '
        'if dist<3 then '
        '  p.walking_state={walking=false} '
        '  rcon.print("DONE "..p.position.x..","..p.position.y) '
        'else '
        '  local angle=math.atan2(dy,dx) '
        '  local deg=(math.deg(angle)+90)%360 '
        '  local dirs={[0]="north",[1]="northeast",[2]="east",[3]="southeast",'
        '[4]="south",[5]="southwest",[6]="west",[7]="northwest"} '
        '  local idx=math.floor((deg/45)+0.5)%8 '
        '  p.walking_state={walking=true,direction=defines.direction[dirs[idx]]} '
        '  rcon.print("WALK "..string.format("%.0f",dist).." "..p.position.x..","..p.position.y) '
        'end'
    )

    t0 = time.time()
    while time.time() - t0 < timeout:
        r = _cmd(rcon, WALK_CMD)
        if r and r.startswith("DONE"):
            pos_str = r.split(" ", 1)[1]
            return f"Arrived at ({pos_str}), walked {dist:.0f} tiles"
        time.sleep(0.05)  # 50ms poll — fast enough for high-speed walking

    _cmd(rcon, 'game.connected_players[1].walking_state={walking=false}')
    pos = _cmd(rcon,
        'local p=game.connected_players[1] '
        'rcon.print(p.position.x..","..p.position.y)')
    return f"Walk timeout, now at ({pos}), target ({x:.0f},{y:.0f})"


def mine_resource(rcon: RCONClient, resource: str, count: int = 5, timeout: float = 30.0) -> str:
    """Mine a resource by hand. Walks to nearest patch and mines."""
    # Find nearest resource
    r = _cmd(rcon,
        f'local p=game.connected_players[1] '
        f'local e=game.surfaces[1].find_entities_filtered{{'
        f'name="{resource}", position=p.position, radius=200, limit=1}} '
        f'if #e>0 then rcon.print(e[1].position.x..","..e[1].position.y) '
        f'else rcon.print("none") end')
    if r == "none" or not r:
        return f"ERROR: no {resource} within 200 tiles"

    tx, ty = [float(v) for v in r.split(",")]

    # Walk close
    walk_result = walk_to(rcon, tx, ty, timeout=10)

    # Start mining
    _cmd(rcon,
        f'local p=game.connected_players[1] '
        f'p.mining_state={{mining=true, position={{{tx},{ty}}}}}')

    # Wait and check inventory
    initial = int(_cmd(rcon,
        f'rcon.print(game.connected_players[1].get_item_count("{resource}"))') or "0")

    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(1)
        current = int(_cmd(rcon,
            f'rcon.print(game.connected_players[1].get_item_count("{resource}"))') or "0")
        mined = current - initial
        if mined >= count:
            _cmd(rcon, 'game.connected_players[1].mining_state={mining=false}')
            return f"Mined {mined} {resource} (now have {current})"

    _cmd(rcon, 'game.connected_players[1].mining_state={mining=false}')
    current = int(_cmd(rcon,
        f'rcon.print(game.connected_players[1].get_item_count("{resource}"))') or "0")
    return f"Mining timeout, got {current - initial} {resource} (now have {current})"


def craft_item(rcon: RCONClient, recipe: str, count: int = 1) -> str:
    """Craft items from inventory."""
    r = _cmd(rcon,
        f'local p=game.connected_players[1] '
        f'local n=p.craft{{recipe="{recipe}", count={count}}} '
        f'rcon.print(n)')
    crafted = int(r) if r and r.isdigit() else 0
    if crafted > 0:
        # Wait for crafting to finish
        time.sleep(0.5 * crafted)
        return f"Crafting {crafted} {recipe}"
    return f"ERROR: could not craft {recipe} (missing ingredients or recipe not unlocked)"


def place_item(rcon: RCONClient, item: str, x: float, y: float, direction: str = "north") -> str:
    """Place an item from inventory at a position."""
    dir_map = {
        "north": "defines.direction.north",
        "south": "defines.direction.south",
        "east": "defines.direction.east",
        "west": "defines.direction.west",
    }
    d = dir_map.get(direction, "defines.direction.north")

    # Check if player has the item
    has = _cmd(rcon, f'rcon.print(game.connected_players[1].get_item_count("{item}"))')
    if not has or has == "0":
        return f"ERROR: don't have {item} in inventory"

    # Walk close first
    walk_to(rcon, x, y, timeout=8)

    # Place using cursor
    r = _cmd(rcon,
        f'local p=game.connected_players[1] '
        f'local stack=p.cursor_stack '
        f'stack.set_stack{{name="{item}", count=1}} '
        f'local ok=p.build_from_cursor{{position={{{x},{y}}}, direction={d}}} '
        f'stack.clear() '
        f'rcon.print(tostring(ok))')
    if r == "true":
        return f"Placed {item} at ({x:.0f},{y:.0f}) facing {direction}"
    return f"ERROR: could not place {item} at ({x:.0f},{y:.0f}) — {r}"


def scan_area(rcon: RCONClient, radius: int = 100) -> str:
    """Scan for resources around the player."""
    r = _cmd(rcon,
        f'local p=game.connected_players[1] '
        f'local px,py=p.position.x,p.position.y '
        f'local t={{}} '
        f'for _,name in pairs({{"iron-ore","copper-ore","coal","stone"}}) do '
        f'  local e=game.surfaces[1].find_entities_filtered{{name=name, '
        f'  position={{px,py}}, radius={radius}}} '
        f'  if #e>0 then '
        f'    local closest=e[1] '
        f'    local min_d=math.huge '
        f'    for _,ent in pairs(e) do '
        f'      local d=math.sqrt((ent.position.x-px)^2+(ent.position.y-py)^2) '
        f'      if d<min_d then min_d=d closest=ent end '
        f'    end '
        f'    t[#t+1]=string.format("%s: %d tiles, nearest (%.0f,%.0f), %d total", '
        f'      name, min_d, closest.position.x, closest.position.y, #e) '
        f'  end '
        f'end '
        f'rcon.print(#t>0 and table.concat(t, "\\n") or "Nothing within {radius} tiles")')
    return r


def run_lua(rcon: RCONClient, code: str) -> str:
    """Execute raw Lua via RCON (escape hatch)."""
    return _cmd(rcon, code)


# ── Cheat actions (RCON-only, no player required) ──────────────────

def unlock_all(rcon: RCONClient) -> str:
    """Research all technologies and enable all recipes for the player force."""
    r = _cmd(rcon,
        'local f=game.forces["player"] '
        'for _,t in pairs(f.technologies) do t.researched=true end '
        'for _,r in pairs(f.recipes) do r.enabled=true end '
        'rcon.print("unlocked "..table_size(f.technologies).." techs, "'
        '..table_size(f.recipes).." recipes")')
    return r or "unlock_all: no response"


def give(rcon: RCONClient, item: str, count: int = 100) -> str:
    """Give items directly to the player inventory (requires connected client)."""
    r = _cmd(rcon,
        f'local p=game.connected_players[1] '
        f'if not p then rcon.print("ERROR: no connected player") '
        f'else local n=p.insert{{name="{item}",count={count}}} '
        f'rcon.print("gave "..n.." "..(n==1 and "{item}" or "{item}s")) end')
    return r or "give: no response"


def teleport(rcon: RCONClient, x: float, y: float) -> str:
    """Teleport the player to a position (requires connected client)."""
    r = _cmd(rcon,
        f'local p=game.connected_players[1] '
        f'if not p then rcon.print("ERROR: no connected player") '
        f'else p.teleport({{{x},{y}}}) '
        f'rcon.print("teleported to ("..p.position.x..","..p.position.y..")") end')
    return r or "teleport: no response"


def create_entity(rcon: RCONClient, name: str, x: float, y: float,
                  direction: str = "north") -> str:
    """Create an entity directly on the surface (no player needed).
    Cheat mode — bypasses inventory and reach distance."""
    dir_map = {
        "north": "defines.direction.north",
        "south": "defines.direction.south",
        "east":  "defines.direction.east",
        "west":  "defines.direction.west",
    }
    d = dir_map.get(direction, "defines.direction.north")
    r = _cmd(rcon,
        f'local e=game.surfaces[1].create_entity{{'
        f'name="{name}",position={{{x},{y}}},'
        f'direction={d},force="player"}} '
        f'if e then rcon.print("created "..e.name.." at ("..e.position.x'
        f'..","..e.position.y..")") '
        f'else rcon.print("ERROR: could not create {name} at ({x},{y})") end')
    return r or "create_entity: no response"


def list_entities(rcon: RCONClient, radius: int = 50) -> str:
    """List player-force entities within radius of origin (or player if connected)."""
    r = _cmd(rcon,
        f'local cx,cy=0,0 '
        f'local p=game.connected_players[1] '
        f'if p then cx,cy=p.position.x,p.position.y end '
        f'local es=game.surfaces[1].find_entities_filtered{{'
        f'force="player",position={{cx,cy}},radius={radius}}} '
        f'local counts={{}} '
        f'for _,e in pairs(es) do '
        f'  counts[e.name]=(counts[e.name] or 0)+1 end '
        f'local t={{}} '
        f'for n,c in pairs(counts) do t[#t+1]=n..":"..c end '
        f'rcon.print(#t>0 and table.concat(t,", ") or "no entities")')
    return r or "list_entities: no response"


# Action registry — maps action names to (function, description)
ACTIONS = {
    "unlock_all": (unlock_all, "unlock_all — Research all tech and enable all recipes (cheat)"),
    "give": (give, "give <item> [count] — Give items to player inventory (cheat, needs client)"),
    "teleport": (teleport, "teleport <x> <y> — Teleport player (needs client)"),
    "create_entity": (create_entity, "create_entity <name> <x> <y> [dir] — Spawn entity directly (cheat, no client needed)"),
    "list_entities": (list_entities, "list_entities [radius] — List your entities around you/origin"),
    "walk_to": (walk_to, "walk_to <x> <y> — Walk to coordinates"),
    "mine": (mine_resource, "mine <resource> [count] — Mine resource by hand (e.g., mine iron-ore 10)"),
    "craft": (craft_item, "craft <recipe> [count] — Craft items (e.g., craft stone-furnace 1)"),
    "place": (place_item, "place <item> <x> <y> [direction] — Place item from inventory"),
    "scan": (scan_area, "scan [radius] — Scan for resources around you"),
    "lua": (run_lua, "lua <code> — Execute raw Lua (advanced)"),
}


def parse_action(text: str) -> tuple[str, list]:
    """Parse an action string like 'mine iron-ore 10' into (action_name, args)."""
    parts = text.strip().split()
    if not parts:
        return "", []
    action = parts[0].lower()
    args = parts[1:]
    return action, args


def execute_action(rcon: RCONClient, action_text: str) -> str:
    """Parse and execute an action. Returns result string."""
    action, args = parse_action(action_text)

    if action == "walk_to" and len(args) >= 2:
        return walk_to(rcon, float(args[0]), float(args[1]))
    elif action == "mine" and len(args) >= 1:
        count = int(args[1]) if len(args) > 1 else 5
        return mine_resource(rcon, args[0], count)
    elif action == "craft" and len(args) >= 1:
        count = int(args[1]) if len(args) > 1 else 1
        return craft_item(rcon, args[0], count)
    elif action == "place" and len(args) >= 3:
        direction = args[3] if len(args) > 3 else "north"
        return place_item(rcon, args[0], float(args[1]), float(args[2]), direction)
    elif action == "scan":
        radius = int(args[0]) if args else 100
        return scan_area(rcon, radius)
    elif action == "lua" and args:
        return run_lua(rcon, " ".join(args))
    elif action == "unlock_all":
        return unlock_all(rcon)
    elif action == "give" and len(args) >= 1:
        count = int(args[1]) if len(args) > 1 else 100
        return give(rcon, args[0], count)
    elif action == "teleport" and len(args) >= 2:
        return teleport(rcon, float(args[0]), float(args[1]))
    elif action == "create_entity" and len(args) >= 3:
        direction = args[3] if len(args) > 3 else "north"
        return create_entity(rcon, args[0], float(args[1]), float(args[2]), direction)
    elif action == "list_entities":
        radius = int(args[0]) if args else 50
        return list_entities(rcon, radius)
    else:
        return f"ERROR: unknown action '{action_text}'. Available: " + ", ".join(ACTIONS.keys())
