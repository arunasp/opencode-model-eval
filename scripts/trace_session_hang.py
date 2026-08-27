#!/usr/bin/env python3
"""trace_session_hang.py -- find out what POST /session is actually
blocked on, rather than guessing at it.

WHY THIS EXISTS. test_run_eval_client_e2e.py has failed intermittently
for months against two documented-but-unconfirmed hypotheses (a blocked
outbound call, a cold npm cache). Both were reasonable and neither was
ever tested, because the one place the answer appears -- opencode's own
log -- is written to $HOME/.local/share/opencode/log/ and the test
discards it, printing only the subprocess pipe, which contains two
lines. Three further guesses were tried and missed. This stage stops
guessing and enumerates instead.

WHAT IT DOES. Starts the real `opencode serve` once per variant, with
`--print-logs --log-level DEBUG` so the log lands in the captured pipe,
fires one POST /session against a deadline, and records what happened.
Each variant toggles exactly one suspected contributor, so a hang that
survives every variant rules them all out and a hang that stops on one
names it:

  baseline          as the test runs it today
  no-models-fetch   OPENCODE_DISABLE_MODELS_FETCH=1 -- populate() returns
                    {} instead of taking a cross-process Flock and
                    fetching a catalog this environment answers 403 for
  pure              --pure, no external plugins loaded
  warm-home         reuses the previous variant's HOME, so the models
                    cache and any other first-run state already exist;
                    the test gives every method a fresh HOME, which
                    guarantees the cold path every single time
  delay-5/15/30s    baseline again, but waiting N seconds after the port
                    opens before posting -- the only variable that
                    changed the outcome

WHAT IT FOUND, 2026-08-27, kept here so the matrix is not re-run blind.
The first four variants ALL hang identically, which rules out every
hypothesis in the e2e test's docstring. The delay variants split
cleanly: 0s blocks past 40s every time, >=5s answers in ~100-340ms. The
port accepts connections roughly 1.5s before the route layer can serve,
and a request landing in that window is accepted, drained out of the
kernel receive buffer, and never answered -- so no finite timeout can
catch it. Full account:
Session-Summaries/20260827-102000-opencode-model-eval-harness-defects.md

EGRESS IS SAMPLED AT THE MOMENT OF THE HANG, not beforehand: a
reachability check run before the request cannot tell you what the
request is waiting on. The container this runs in can see both
opencode's log and the host's Ollama, so both are recorded together.

Exit codes follow tools/pipeline.sh's convention:
  0  every variant created a session -- nothing to trace
  1  at least one variant hung; the artifact names which and what it logged
  2  SKIPPED -- node/npm absent, so no real opencode can be started
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

RESULTS_DIR = Path(os.environ.get("TRACE_RESULTS_DIR", REPO_ROOT / "results" / "trace"))
# Pinned to whatever the e2e test pins, so this traces THAT failure and
# not a different build's behaviour. Read from the test rather than
# duplicated, for the same reason the probe imports its session helpers.
OPENCODE_VERSION = os.environ.get("TRACE_OPENCODE_VERSION", "1.18.3")
SESSION_DEADLINE_S = int(os.environ.get("TRACE_SESSION_DEADLINE_S", "45"))
PORT_DEADLINE_S = int(os.environ.get("TRACE_PORT_DEADLINE_S", "30"))
SAMPLE_INTERVAL_S = 5

# name, extra env, extra argv, fresh HOME, seconds to wait after the port
# opens before posting. The DELAY column is the one that settled it -- the
# first four variants each toggle a suspected contributor and all four hang
# identically, which is what ruled them out; the last three vary only the
# delay and split cleanly at ~5s.
VARIANTS = [
    ("baseline", {}, [], True, 0),
    ("no-models-fetch", {"OPENCODE_DISABLE_MODELS_FETCH": "1"}, [], True, 0),
    ("pure", {}, ["--pure"], True, 0),
    ("warm-home", {}, [], False, 0),
    ("delay-5s", {}, [], True, 5),
    ("delay-15s", {}, [], True, 15),
    ("delay-30s", {}, [], True, 30),
]


def log(msg):
    print(f"[trace] {msg}", flush=True)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def node_npm_available() -> bool:
    return shutil.which("node") is not None and shutil.which("npm") is not None


def probe(url, timeout=5):
    """Reachability plus timing. Any HTTP answer is an answer."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return {"url": url, "status": resp.status, "elapsed_s": round(time.monotonic() - started, 3)}
    except urllib.error.HTTPError as exc:
        return {"url": url, "status": exc.code, "elapsed_s": round(time.monotonic() - started, 3)}
    except Exception as exc:
        return {"url": url, "error": f"{type(exc).__name__}: {exc}",
                "elapsed_s": round(time.monotonic() - started, 3)}


def egress_snapshot():
    """What this container can reach, sampled when it matters.

    The catalog endpoints are the ones opencode fetches at startup;
    Ollama is here because this container can see it and the e2e stage
    already depends on it, so a single artifact covers both.
    """
    try:
        from hostnet import host_candidates
        ollama = host_candidates(11434, "/api/tags", os.environ.get("OPENCODE_OLLAMA_TAGS_URL", ""))[0]
    except Exception:
        ollama = "http://localhost:11434/api/tags"
    return [
        probe("https://models.dev/api.json"),
        probe("https://models.opencode.ai/api.json"),
        probe("https://registry.npmjs.org/"),
        probe(ollama),
    ]


def install_opencode(install_dir: Path) -> Path:
    """Install once and reuse. The test reinstalls per method, which is
    what made 'cold npm cache' plausible and untestable at the same
    time -- here the install is outside the timed region entirely.
    """
    binary = install_dir / "node_modules" / ".bin" / "opencode"
    if binary.exists():
        log(f"reusing existing install at {binary}")
        return binary
    install_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    subprocess.run(
        ["npm", "install", f"opencode-ai@{OPENCODE_VERSION}"],
        cwd=install_dir, check=True, capture_output=True, timeout=300,
    )
    log(f"installed opencode-ai@{OPENCODE_VERSION} in {round(time.monotonic() - started, 1)}s")
    if not binary.exists():
        raise RuntimeError(f"opencode binary not found at {binary}")
    return binary


def write_config(path: Path, mock_port: int) -> None:
    path.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "model": "mock/mock-model",
        "permission": {"edit": "deny", "bash": "deny"},
        "provider": {
            "mock": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Mock",
                "options": {"baseURL": f"http://127.0.0.1:{mock_port}/v1", "apiKey": "mock"},
                "models": {"mock-model": {}},
            }
        },
    }, indent=2))


def wait_for_port(port: int, deadline_s: int):
    started = time.monotonic()
    while time.monotonic() - started < deadline_s:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return round(time.monotonic() - started, 3)
        except OSError:
            time.sleep(0.05)
    return None


def post_session(base_url: str, out: dict):
    """Runs in a worker thread so the main thread can keep sampling
    while it is blocked. No timeout on the request itself -- the point
    is to observe the block, and the caller enforces the deadline.
    """
    started = time.monotonic()
    req = urllib.request.Request(f"{base_url}/session", method="POST",
                                 data=b"{}", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=SESSION_DEADLINE_S * 4) as resp:
            out["status"] = resp.status
            out["body"] = resp.read(400).decode("utf-8", "replace")
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["elapsed_s"] = round(time.monotonic() - started, 3)


def run_variant(name, extra_env, extra_args, fresh_home, delay_s, binary: Path, scratch: Path) -> dict:
    log(f"--- variant: {name}")
    home = scratch / ("home-" + name if fresh_home else "home-shared")
    if fresh_home and home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True, exist_ok=True)

    config = scratch / f"config-{name}.json"
    write_config(config, free_port())
    serve_port = free_port()
    log_path = RESULTS_DIR / f"{name}-serve.log"

    env = {
        **os.environ,
        "HOME": str(home),
        "OPENCODE_CONFIG": str(config),
        # The whole point: put opencode's own log where the failure path
        # can actually read it.
        "OPENCODE_PRINT_LOGS": "1",
        "OPENCODE_LOG_LEVEL": "DEBUG",
        **extra_env,
    }
    result = {"variant": name, "extra_env": extra_env, "extra_args": extra_args,
              "fresh_home": fresh_home, "serve_log": str(log_path)}

    with log_path.open("w") as sink:
        proc = subprocess.Popen(
            [str(binary), "serve", "--port", str(serve_port), "--hostname", "127.0.0.1",
             "--print-logs", "--log-level", "DEBUG", *extra_args],
            cwd=str(scratch), env=env, stdout=sink, stderr=subprocess.STDOUT, text=True,
        )
        try:
            port_s = wait_for_port(serve_port, PORT_DEADLINE_S)
            result["port_open_s"] = port_s
            if port_s is None:
                result["outcome"] = "PORT_NEVER_OPENED"
                return result
            if delay_s:
                log(f"    port open in {port_s}s, waiting {delay_s}s before posting")
                time.sleep(delay_s)
            result["delay_s"] = delay_s
            log("    posting /session")

            session = {}
            worker = threading.Thread(target=post_session,
                                      args=(f"http://127.0.0.1:{serve_port}", session), daemon=True)
            worker.start()

            samples = []
            waited = 0.0
            while worker.is_alive() and waited < SESSION_DEADLINE_S:
                time.sleep(SAMPLE_INTERVAL_S)
                waited += SAMPLE_INTERVAL_S
                sink.flush()
                samples.append({
                    "at_s": waited,
                    "log_bytes": log_path.stat().st_size,
                    "log_tail": tail_lines(log_path, 3),
                })
                log(f"    +{int(waited)}s still waiting, log={samples[-1]['log_bytes']}B")
            result["samples"] = samples

            if worker.is_alive():
                # Sampled HERE, while blocked -- a check run before the
                # request cannot say what the request is waiting on.
                result["outcome"] = "HUNG"
                result["egress_during_hang"] = egress_snapshot()
                log(f"    HUNG after {SESSION_DEADLINE_S}s")
            else:
                result.update(session)
                result["outcome"] = "OK" if session.get("status") == 200 else "FAILED"
                log(f"    {result['outcome']} in {session.get('elapsed_s')}s")
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.wait(timeout=5)

    result["serve_log_bytes"] = log_path.stat().st_size
    result["serve_log_tail"] = tail_lines(log_path, 25)
    return result


def tail_lines(path: Path, count: int):
    try:
        return path.read_text(errors="replace").splitlines()[-count:]
    except OSError:
        return []


def main():
    if not node_npm_available():
        log("node/npm not on PATH -- cannot start a real opencode serve, skipping")
        return 2

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    scratch = RESULTS_DIR / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "opencode_version": OPENCODE_VERSION,
        "session_deadline_s": SESSION_DEADLINE_S,
        "egress_before": egress_snapshot(),
        "variants": [],
    }

    binary = install_opencode(scratch / "install")
    for name, extra_env, extra_args, fresh_home, delay_s in VARIANTS:
        report["variants"].append(
            run_variant(name, extra_env, extra_args, fresh_home, delay_s, binary, scratch))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS_DIR / f"{stamp}-session-trace.json"
    out.write_text(json.dumps(report, indent=2) + "\n")

    log("")
    log("SUMMARY")
    for v in report["variants"]:
        log(f"  {v['variant']:<16} {v['outcome']:<18} "
            f"delay={v.get('delay_s')}s port={v.get('port_open_s')}s session={v.get('elapsed_s')}s "
            f"log={v.get('serve_log_bytes')}B")
    log(f"artifact: {out}")

    hung = [v for v in report["variants"] if v["outcome"] != "OK"]
    return 1 if hung else 0


if __name__ == "__main__":
    sys.exit(main())
