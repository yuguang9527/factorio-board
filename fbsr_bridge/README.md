# FBSR Bridge — Headless Factorio Rendering

Pipeline: **live Factorio server → RCON blueprint export → Redis queue → warm-JVM FBSR daemon → PNG**. No Factorio client required.

## Components

| File | Role |
|------|------|
| `RenderBP.java` | Java daemon that keeps FBSR loaded and renders BP strings from stdin |
| `fbsr_worker.py` | Redis consumer — pops `fbsr:jobs`, pipes to the Java daemon |
| `fbsr_client.py` | Agent-side helpers: `export_blueprint(rcon)`, `enqueue_render(bp, out)`, `wait_for_render(id)` |

## One-time setup (done)

- Clone + `mvn install` the three repos: `Java-Factorio-Data-Wrapper`, `Discord-Core-Bot-Apple`, `Factorio-FBSR`. All under `~/fbsr/`.
- `pom.xml` patched: `org.sejda.imageio:webp-imageio:0.1.6` → `com.github.usefulness:webp-imageio:0.10.1` (arm64 Mac native lib).
- `config.json` points to `/Applications/factorio.app/Contents`.
- `profiles/vanilla/profile.json` stripped to `["base"]` (no Space Age DLC installed).
- `mvn package -DskipTests` → `target/FactorioBlueprintStringRenderer-0.0.1-SNAPSHOT.jar`.
- **Must run via `java -cp ...`, not `mvn exec:java`** (mvn exec classloader breaks WebP SPI).
- `java -cp ... com.demod.fbsr.FBSRMain build vanilla` — extracted 7764 sprites into `build/vanilla/vanilla.zip`.
- Classpath cached at `/tmp/fbsr-cp.txt` (from `mvn dependency:build-classpath`).

## Runtime

```bash
# 1. Redis
brew services start redis

# 2. FBSR worker (stays warm)
python3 ~/wandb-factorio_v2/fbsr_bridge/fbsr_worker.py

# 3. agent_loop.py — take_screenshot() now enqueues to fbsr:jobs
python3 -u agent_loop.py
```

## Performance

- JVM cold start (first render): ~7s
- Warm render via queue: **~100ms/frame**
- ~65× speedup from keeping FBSR loaded

## Queue protocol

- `fbsr:jobs` (list, LPUSH/BRPOP): `{"id": str, "bp": "0eNp...", "out": "/abs/path.png"}`
- `fbsr:done:<id>` (TTL 60s): `"OK"` or `"ERR ..."`
- `fbsr:worker:heartbeat` (TTL 30s): unix ts

## Known gotchas

- Logback writes to stdout; worker drains lines until it sees `OK `/`ERR ` sentinel.
- `helpers.write_file` replaced `game.write_file` in Factorio 2.0 (unrelated to FBSR but needed for other server-side dumps).
- `game.take_screenshot` silently fails on dedicated servers (no OpenGL context) — that's why we use this pipeline instead.
