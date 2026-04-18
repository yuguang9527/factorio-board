"""High-level Factorio tools — intention-level, not coord-level.

Inspired by FLE but trimmed: 8 tools that cover iron→science chain construction
without forcing the LLM to compute tile adjacency or belt pathfinding.

LLM writes short Python programs using these in a per-step REPL.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Literal, Optional

from factorio_rcon import RCONClient

Direction = Literal["north", "east", "south", "west"]
_DIR_DELTA = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
_OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east"}


@dataclass(frozen=True)
class Pos:
    x: float
    y: float

    def __add__(self, other: "Pos") -> "Pos":
        return Pos(self.x + other.x, self.y + other.y)

    def dist(self, other: "Pos") -> float:
        return math.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2)


@dataclass
class Ent:
    name: str
    pos: Pos
    direction: Direction
    unit_number: int

    def __repr__(self) -> str:
        return f"Ent({self.name}@{self.pos.x:.0f},{self.pos.y:.0f} {self.direction})"


# Tile footprint per entity (width, height). Used for place_next_to.
_FOOTPRINT = {
    "burner-mining-drill": (2, 2),
    "stone-furnace": (2, 2),
    "burner-inserter": (1, 1),
    "inserter": (1, 1),
    "wooden-chest": (1, 1),
    "iron-chest": (1, 1),
    "steel-chest": (1, 1),
    "assembling-machine-1": (3, 3),
    "assembling-machine-2": (3, 3),
    "assembling-machine-3": (3, 3),
    "transport-belt": (1, 1),
    "fast-transport-belt": (1, 1),
    "boiler": (3, 2),
    "steam-engine": (5, 3),
    "pipe": (1, 1),
    "offshore-pump": (1, 1),
    "small-electric-pole": (1, 1),
    "medium-electric-pole": (1, 1),
}


class World:
    """Facade over a Factorio RCON connection with intention-level tools."""

    def __init__(self, rcon: RCONClient):
        self.rcon = rcon

    # ── low-level helper ───────────────────────────────────────────
    def _lua(self, code: str) -> str:
        return (self.rcon.send_command("/sc " + code) or "").strip()

    def _json(self, code: str):
        """Execute Lua that prints a JSON string; decode."""
        s = self._lua(code)
        if not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None

    def _ent_from_unit_number(self, un: int) -> Optional[Ent]:
        if un <= 0:
            return None
        row = self._json(
            f"local e=nil for _,x in pairs(game.surfaces[1].find_entities_filtered{{force='player'}}) do "
            f"if x.unit_number=={un} then e=x break end end "
            f"if not e then rcon.print('null') "
            f"else rcon.print(helpers.table_to_json{{name=e.name,x=e.position.x,y=e.position.y,"
            f"dir=e.direction,un=e.unit_number}}) end"
        )
        if not row:
            return None
        return Ent(row["name"], Pos(row["x"], row["y"]),
                   _dir_from_int(row["dir"]), row["un"])

    # ── 1. NEAREST ─────────────────────────────────────────────────
    def nearest(self, resource: str, origin: Pos = Pos(0, 0), radius: int = 200) -> Optional[Pos]:
        """Find closest tile of `resource` within `radius` of `origin`."""
        r = self._lua(
            f"local e=game.surfaces[1].find_entities_filtered"
            f"{{name='{resource}',position={{{origin.x},{origin.y}}},radius={radius}}} "
            f"if #e==0 then rcon.print('none') return end "
            f"local best,min=e[1],math.huge "
            f"for _,x in pairs(e) do local d=math.sqrt((x.position.x-{origin.x})^2+"
            f"(x.position.y-{origin.y})^2) if d<min then min=d best=x end end "
            f"rcon.print(best.position.x..','..best.position.y)"
        )
        if not r or r == "none":
            return None
        x, y = r.split(",")
        return Pos(float(x), float(y))

    # ── 2. PLACE ───────────────────────────────────────────────────
    def place(self, entity: str, at: Pos, direction: Direction = "north") -> Optional[Ent]:
        """Spawn `entity` at `at` facing `direction`. Cheat-mode (no inventory)."""
        un = self._lua(
            f"local e=game.surfaces[1].create_entity{{name='{entity}',"
            f"position={{{at.x},{at.y}}},direction=defines.direction.{direction},"
            f"force='player'}} "
            f"if e then rcon.print(e.unit_number) else rcon.print(0) end"
        )
        try:
            un_int = int(un)
        except (ValueError, TypeError):
            return None
        return self._ent_from_unit_number(un_int)

    # ── 3. PLACE_NEXT_TO ───────────────────────────────────────────
    def place_next_to(self, entity: str, ref: Ent, direction: Direction,
                      facing: Optional[Direction] = None) -> Optional[Ent]:
        """Place `entity` adjacent to `ref`, on the given side.
        `facing` defaults to pointing back toward `ref`."""
        w_ref, h_ref = _FOOTPRINT.get(ref.name, (1, 1))
        w_new, h_new = _FOOTPRINT.get(entity, (1, 1))
        dx, dy = _DIR_DELTA[direction]

        # Step out by the half-sizes plus one tile for the offset
        step_x = (w_ref + w_new) / 2 * abs(dx)
        step_y = (h_ref + h_new) / 2 * abs(dy)
        new_pos = Pos(ref.pos.x + dx * step_x, ref.pos.y + dy * step_y)

        facing = facing or _OPPOSITE[direction]
        return self.place(entity, new_pos, facing)

    # ── 4. CONNECT_BELT ────────────────────────────────────────────
    def connect_belt(self, src: Pos, dst: Pos, belt: str = "transport-belt") -> int:
        """Place a straight L-shaped belt from src to dst. Returns tile count.
        No obstacle avoidance — caller must ensure path is clear."""
        # horizontal leg first, then vertical
        x1, y1 = int(round(src.x)), int(round(src.y))
        x2, y2 = int(round(dst.x)), int(round(dst.y))
        placed = 0
        dx = 1 if x2 > x1 else -1
        for x in range(x1, x2, dx):
            direction = "east" if dx > 0 else "west"
            if self.place(belt, Pos(x + 0.5, y1 + 0.5), direction):
                placed += 1
        dy = 1 if y2 > y1 else -1
        for y in range(y1, y2, dy):
            direction = "south" if dy > 0 else "north"
            if self.place(belt, Pos(x2 + 0.5, y + 0.5), direction):
                placed += 1
        return placed

    # ── 5. INSERT ──────────────────────────────────────────────────
    def insert(self, entity: Ent, item: str, count: int) -> int:
        """Insert items into an entity's inventory. Returns count actually inserted."""
        r = self._lua(
            f"local es=game.surfaces[1].find_entities_filtered"
            f"{{position={{{entity.pos.x},{entity.pos.y}}},radius=0.5}} "
            f"local e=nil for _,x in pairs(es) do if x.unit_number=={entity.unit_number} then e=x break end end "
            f"if not e then rcon.print(0) return end "
            f"local n=e.insert{{name='{item}',count={count}}} "
            f"rcon.print(n)"
        )
        try:
            return int(r)
        except (ValueError, TypeError):
            return 0

    # ── 6. INVENTORY ───────────────────────────────────────────────
    def inventory(self, entity: Ent) -> dict[str, int]:
        """Return {item_name: count} for all inventory slots of `entity`."""
        j = self._json(
            f"local es=game.surfaces[1].find_entities_filtered"
            f"{{position={{{entity.pos.x},{entity.pos.y}}},radius=0.5}} "
            f"local e=nil for _,x in pairs(es) do if x.unit_number=={entity.unit_number} then e=x break end end "
            f"if not e then rcon.print('{{}}') return end "
            f"local t={{}} "
            f"for i=1,e.get_max_inventory_index() do "
            f"  local inv=e.get_inventory(i) "
            f"  if inv then for k,v in pairs(inv.get_contents()) do t[k]=(t[k] or 0)+v end end "
            f"end "
            f"rcon.print(helpers.table_to_json(t))"
        )
        return j or {}

    # ── 7. GET_ENTITIES ────────────────────────────────────────────
    def get_entities(self, within: float = 50, origin: Pos = Pos(0, 0)) -> list[Ent]:
        """List all player-force entities in a square of side `2*within` around `origin`."""
        j = self._json(
            f"local es=game.surfaces[1].find_entities_filtered"
            f"{{force='player',position={{{origin.x},{origin.y}}},radius={within}}} "
            f"local t={{}} "
            f"for _,e in pairs(es) do "
            f"  if e.name~='character' and not e.name:find('crash') then "
            f"    t[#t+1]={{name=e.name,x=e.position.x,y=e.position.y,dir=e.direction,un=e.unit_number}} "
            f"  end "
            f"end "
            f"rcon.print(helpers.table_to_json(t))"
        )
        return [Ent(r["name"], Pos(r["x"], r["y"]), _dir_from_int(r["dir"]), r["un"])
                for r in (j or [])]

    # ── 8. SCORE ───────────────────────────────────────────────────
    def score(self) -> float:
        """Total items produced on nauvis (production statistic)."""
        r = self._lua(
            "local s=game.forces['player'].get_item_production_statistics('nauvis') "
            "local t=0 for _,v in pairs(s.input_counts) do t=t+v end "
            "rcon.print(t)"
        )
        try:
            return float(r)
        except (ValueError, TypeError):
            return 0.0


def _dir_from_int(d: int) -> Direction:
    # Factorio 2.0: 0=N, 4=E, 8=S, 12=W (step 2 for 8-way, only cardinal used here)
    if d == 0:
        return "north"
    if d == 4:
        return "east"
    if d == 8:
        return "south"
    if d == 12:
        return "west"
    # 2, 6, 10, 14 are intercardinal — round down to the preceding cardinal
    return ("north", "east", "south", "west")[(d // 4) % 4]
