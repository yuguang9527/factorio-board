#!/usr/bin/env python3
"""FBSR render worker.

Reads BP render jobs from Redis list 'fbsr:jobs', feeds them to a warm JVM
running RenderBP in daemon mode, writes PNGs to disk, and reports completion
via 'fbsr:done:<id>' keys (TTL 60s).

Job payload (JSON): {"id": str, "bp": str, "out": absolute_png_path}
"""
import json
import os
import subprocess
import sys
import time

import redis

FBSR_HOME = os.path.expanduser(
    "~/fbsr/Factorio-FBSR/FactorioBlueprintStringRenderer"
)
BRIDGE = os.path.expanduser("~/wandb-factorio_v2/fbsr_bridge")
CP_CACHE = "/tmp/fbsr-cp.txt"


def build_classpath() -> str:
    if not os.path.exists(CP_CACHE):
        subprocess.check_call(
            ["mvn", "dependency:build-classpath", "-q",
             f"-Dmdep.outputFile={CP_CACHE}"],
            cwd=FBSR_HOME,
        )
    with open(CP_CACHE) as f:
        base = f.read().strip()
    jar = f"{FBSR_HOME}/target/FactorioBlueprintStringRenderer-0.0.1-SNAPSHOT.jar"
    return f"{base}:{FBSR_HOME}/target/classes:{jar}:{BRIDGE}"


def main():
    profile = os.environ.get("FBSR_PROFILE", "vanilla")
    r = redis.Redis(decode_responses=True)
    r.ping()

    cp = build_classpath()
    print(f"→ starting FBSR daemon (profile={profile})", flush=True)
    proc = subprocess.Popen(
        ["java", "-cp", cp, "RenderBP", "-daemon", profile],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
        text=True, bufsize=1, cwd=FBSR_HOME,
    )

    while True:
        line = proc.stdout.readline()
        if not line:
            print("⚠️  daemon exited before READY", flush=True)
            return 1
        if line.strip() == "READY":
            print("✅ FBSR daemon ready", flush=True)
            break
        print(f"  {line.rstrip()}", flush=True)

    print("→ listening on redis list 'fbsr:jobs'", flush=True)
    try:
        while True:
            r.set("fbsr:worker:heartbeat", str(int(time.time())), ex=30)
            popped = r.brpop("fbsr:jobs", timeout=10)
            if popped is None:
                continue
            _, raw = popped
            try:
                job = json.loads(raw)
            except json.JSONDecodeError:
                print(f"⚠️  bad job json: {raw[:100]}", flush=True)
                continue

            job_id = job.get("id", "<no-id>")
            bp = job["bp"]
            out = job["out"]

            t0 = time.time()
            proc.stdin.write(f"{out}\t{bp}\n")
            proc.stdin.flush()
            # Drain logback noise until we see our sentinel line
            result = ""
            while True:
                line = proc.stdout.readline()
                if not line:
                    result = "ERR daemon closed stdout"
                    break
                line = line.strip()
                if line.startswith("OK ") or line.startswith("ERR ") or line == "OK":
                    result = line
                    break
            dt = (time.time() - t0) * 1000

            if result.startswith("OK"):
                r.set(f"fbsr:done:{job_id}", "OK", ex=60)
                print(f"✅ [{job_id}] {out} ({dt:.0f}ms)", flush=True)
            else:
                r.set(f"fbsr:done:{job_id}", result, ex=60)
                print(f"❌ [{job_id}] {result}", flush=True)
    except KeyboardInterrupt:
        print("\n→ shutting down", flush=True)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
