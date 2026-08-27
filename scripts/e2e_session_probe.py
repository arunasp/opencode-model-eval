#!/usr/bin/env python3
"""Opens one real session against a running `opencode serve`, sends a
single "hi", closes it, and records what happened.

This is the smallest end-to-end check that exercises the whole path a
real eval run uses -- HTTP reachability, session creation, the model
reference being accepted by the provider, a reply coming back, and the
session being closed again -- without running the test ladder or
scoring anything.

The session helpers are imported from run_eval_client.py rather than
reimplemented. The request/response schemas there are confirmed against
opencode's own source AND empirically against a real serve instance;
a second copy of them in this file would be a second thing to keep in
step, and this repo has already been bitten by exactly that (three
independent copies of the same wrong log-mount assumption).

Importing it is not inert: run_eval_client.py installs SIGINT/SIGTERM
handlers at module level. They act on its own run-state dict, which
stays empty here, so they are harmless for a probe this short-lived --
noted because it is a side effect, not because it causes a problem.

Exit codes follow tools/pipeline.sh's convention:
  0  the session opened, replied and closed
  2  SKIPPED -- no server reachable, or no provider/model selected
  1  a real failure
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_eval_client import (  # noqa: E402
    abort_session,
    create_session,
    extract_reply,
    http_get,
    send_message,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(os.environ.get("E2E_RESULTS_DIR", REPO_ROOT / "results" / "e2e-session"))
DISCOVERED_ENV = REPO_ROOT / "results" / "discovered" / "discovered-model.env"
PROMPT = "hi"
SKIP = 2
# Short enough that a genuinely absent server is reported quickly, long
# enough that a healthy one answers within it.
SWEEP_TIMEOUT_S = 5
# How often to re-sweep while waiting for a server to BIND. Only used
# when the caller started the stack itself (--require-server); a bind is
# a cheap check, unlike the readiness wait below it.
BIND_POLL_INTERVAL_S = 2


def log(msg):
    print(f"[e2e-session] {msg}", flush=True)


def default_gateway():
    """The host's address as seen from inside a container. A cicd_runner
    worker joins the default bridge with no --add-host, so
    host.docker.internal does not resolve there -- the route table does.
    """
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and fields[1] == "00000000":
                hex_ip = fields[2]
                return ".".join(str(int(hex_ip[i:i + 2], 16)) for i in (6, 4, 2, 0))
    except OSError:
        pass
    return None


def candidate_urls(explicit):
    if explicit:
        return [explicit]
    port = os.environ.get("OPENCODE_SERVE_PORT", "49605")
    urls = [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]
    gateway = default_gateway()
    if gateway:
        urls.append(f"http://{gateway}:{port}")
    urls.append("http://server:4096")
    return urls


def probe(base_url, timeout):
    """One attempt. Returns 'up', 'absent' or 'slow'.

    'slow' means the connection was accepted and the answer did not
    arrive in time -- the request is still being served after this
    returns, which is the whole reason the caller must not simply fire
    another one.
    """
    try:
        urllib.request.urlopen(f"{base_url}/session", timeout=timeout)
        return "up"
    except urllib.error.HTTPError:
        return "up"
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
            return "absent"
        if isinstance(reason, TimeoutError):
            return "slow"
        return "absent"
    except TimeoutError:
        return "slow"
    except Exception:
        return "absent"


def wait_for_server(candidates, ready_timeout, expect_server=False):
    """Resolve which server to talk to, waiting for a cold one.

    Three states, because 'nothing is listening', 'not listening YET'
    and 'listening but still bootstrapping' need different treatment and
    a single-shot check cannot tell them apart.

    A quick sweep first: a refused connection or an unresolvable name is
    a real answer WHEN NOBODY CLAIMS TO HAVE STARTED ONE, so a genuinely
    absent server is reported in seconds rather than after the full
    deadline.

    expect_server INVERTS that reading. A caller that has just started
    the stack knows a server is coming, so a refused connection means
    NOT BOUND YET, not absent -- the process may still be doing startup
    work before it binds, and this harness's own entrypoint does exactly
    that (`opencode models --refresh` runs before `serve`, bounded at
    60s). Treating refusal as absence there fails the moment the machine
    is slower or its caches colder than the one the check was written
    on. Measured: green locally with a warm npm cache, failed on a
    GitHub runner where the same warm-up had to fetch everything.

    Once any candidate is merely slow, this waits on ONE long request
    rather than issuing a new short one every few seconds. A request
    abandoned at the client is not abandoned at the server -- it goes on
    creating an instance, and polling it repeatedly stacks more of that
    work onto a server already busy with the last attempt. Waiting is
    what makes the readiness check stop competing with the readiness it
    is waiting for.
    """
    deadline = time.monotonic() + ready_timeout
    announced = False
    while True:
        for candidate in candidates:
            state = probe(candidate, timeout=SWEEP_TIMEOUT_S)
            if state == "up":
                return candidate.rstrip("/")
            if state == "slow":
                log(f"{candidate} is listening but still coming up -- "
                    f"waiting up to {ready_timeout}s")
                while time.monotonic() < deadline:
                    remaining = max(1, int(deadline - time.monotonic()))
                    if probe(candidate, timeout=remaining) == "up":
                        return candidate.rstrip("/")
                log(f"{candidate} never became ready within {ready_timeout}s")
                return ""

        if not expect_server:
            return ""
        if time.monotonic() >= deadline:
            log(f"no candidate bound a port within {ready_timeout}s")
            return ""
        if not announced:
            log("nothing listening yet, and a server was started -- "
                f"waiting up to {ready_timeout}s for it to bind")
            announced = True
        time.sleep(BIND_POLL_INTERVAL_S)


def resolve_model():
    provider = os.environ.get("OPENCODE_MODEL_PROVIDER", "")
    model_id = os.environ.get("OPENCODE_MODEL_ID", "")
    source = "environment"
    if not provider or not model_id:
        if DISCOVERED_ENV.is_file():
            values = {}
            for line in DISCOVERED_ENV.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip().strip('"').strip("'")
            provider = provider or values.get("OPENCODE_MODEL_PROVIDER", "")
            model_id = model_id or values.get("OPENCODE_MODEL_ID", "")
            source = f"{DISCOVERED_ENV.relative_to(REPO_ROOT)}"
    return provider, model_id, source


def session_ids(base_url):
    """GET /session/status returns a map keyed by session id."""
    try:
        statuses = http_get(base_url, "/session/status")
    except Exception:
        return None
    if isinstance(statuses, dict):
        return set(statuses.keys())
    return None


def delete_session(base_url, session_id):
    """Cleanup beyond abort, when the server supports it. Probed rather
    than assumed: a 404/405 means this build has no such route, which is
    recorded in the result instead of failing the run.
    """
    req = urllib.request.Request(f"{base_url}/session/{session_id}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True, "deleted"
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 405, 501):
            return False, f"not supported (HTTP {exc.code})"
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("OPENCODE_SERVER_URL", ""))
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--ready-timeout", type=int,
                        default=int(os.environ.get("E2E_READY_TIMEOUT_S", "120")),
                        help="how long to wait for a server that is listening but still starting")
    parser.add_argument("--check-only", action="store_true",
                        help="resolve and report a reachable server, then exit without opening a session")
    parser.add_argument("--require-server", action="store_true",
                        help=("treat an unreachable server as a failure rather than a skip, "
                              "for a caller that just started one"))
    args = parser.parse_args()

    candidates = candidate_urls(args.base_url)
    base_url = wait_for_server(candidates, args.ready_timeout,
                               expect_server=args.require_server)
    if not base_url:
        log("no opencode serve instance answered on any candidate URL")
        log(f"tried: {', '.join(candidates)}")
        # A caller that has just started the stack knows one should be
        # there, so absence is a real failure for it and a skip for
        # everyone else.
        return 1 if args.require_server else SKIP

    if args.check_only:
        log(f"server reachable: {base_url}")
        return 0

    provider, model_id, source = resolve_model()
    if not provider or not model_id:
        log("no provider/model selected (set OPENCODE_MODEL_PROVIDER and "
            "OPENCODE_MODEL_ID, or run discovery first) -- skipping")
        return SKIP

    log(f"server: {base_url}")
    log(f"model: {provider}/{model_id} (from {source})")

    result = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "provider": provider,
        "model_id": model_id,
        "model_source": source,
        "prompt": PROMPT,
    }
    started = time.monotonic()
    session_id = None
    rc = 0
    try:
        before = session_ids(base_url)
        session_id = create_session(base_url)
        result["session_id"] = session_id
        log(f"session opened: {session_id}")

        response = send_message(base_url, session_id, provider, model_id, PROMPT,
                                timeout=args.timeout)
        reply, _parts = extract_reply(response)
        result["reply_chars"] = len(reply)
        result["reply_preview"] = reply[:200]
        result["finish"] = response.get("info", {}).get("finish")
        log(f"reply: {len(reply)} chars, finish={result['finish']}")
        if not reply.strip():
            log("FAIL: the session returned an empty reply")
            rc = 1
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        log(f"FAIL: {result['error']}")
        rc = 1
    finally:
        if session_id:
            result["aborted"] = abort_session(base_url, session_id)
            deleted, detail = delete_session(base_url, session_id)
            result["deleted"] = deleted
            result["delete_detail"] = detail
            log(f"closed: abort={result['aborted']} delete={detail}")
            after = session_ids(base_url)
            if before is not None and after is not None:
                result["leaked_sessions"] = sorted(after - before - {session_id})
                if result["leaked_sessions"]:
                    log(f"FAIL: sessions left behind: {result['leaked_sessions']}")
                    rc = 1
        result["elapsed_s"] = round(time.monotonic() - started, 2)
        result["result"] = "PASS" if rc == 0 else "FAIL"
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = RESULTS_DIR / f"{stamp}-session-probe.json"
        out.write_text(json.dumps(result, indent=2) + "\n")
        log(f"result written: {out}")

    return rc


if __name__ == "__main__":
    sys.exit(main())
