#!/usr/bin/env python3
"""session_reaper.py -- server-side session TTL for opencode.

opencode has no native session TTL/idle-expiry mechanism -- confirmed
by source search across packages/opencode/src/session/ (session.ts,
processor.ts, status.ts, run-state.ts): nothing named ttl/idle-timeout/
expire/reaper exists there. This fills that gap from outside, running
as a background loop alongside `opencode serve` inside the same
container (reaches it over localhost, not host.docker.internal).

Exists specifically for ABRUPT client disconnection: run_eval_client.py
now aborts its own sessions on every path (see the run_category() fix
this session), but that only covers the client being alive to run its
own cleanup. If the eval-client process itself is killed, crashes, or
the container running it is torn down mid-tier, nothing tells opencode
to stop -- its own internal retry (session/retry.ts, confirmed
unbounded) just keeps going, which is exactly what kept Ollama
persistently loaded even after the client producing the load was gone.
This is the safety net for that case specifically, independent of
however well-behaved any given client is.

PROVIDER-SCOPED TTL -- confirmed reliable via source, not guessed:
Session.Info.model (providerID/id/variant) starts unset on a session
created the way run_eval_client.py does it (empty-body POST /session),
but gets patched in unconditionally on the FIRST /session/{id}/message
call. Confirmed at session/prompt.ts:672-689: the incoming message's
model is compared against `current.model?.providerID` etc, and since
that starts undefined, the comparison always differs on message one,
so `sessions.setAgentModel()` always fires and writes model.providerID
onto the session record before the model is ever actually queried.
This means GET /session's response reliably carries model.providerID
for any session that has sent at least one message -- enough to apply
a separate, more aggressive TTL to local/ollama sessions specifically
(the actual resource cost -- a stuck/abandoned local model staying
loaded) without also cutting off legitimate cloud quota-retry waits,
which run_eval_client.py's own QUOTA_WAIT_THRESHOLD_S already patiently
waits on for up to 50min (OPENCODE_QUOTA_WAIT_THRESHOLD_S, 3000s
default). LOCAL_PROVIDER_KEY reuses OPENCODE_OLLAMA_PROVIDER_KEY,
the same env var entrypoint.sh/discover_local_ollama_models.py already
use for this (default "local/ollama", config/opencode.base.json's own
provider key).

Sessions with no model set at all (created but never sent a first
message) fall back to OPENCODE_SESSION_TTL_S -- there's no provider to
scope by yet, and a session stuck at that stage isn't consuming any
provider resource regardless, so the generic default is fine there.

REQUEST/RESPONSE SCHEMA -- confirmed from opencode's actual source,
not guessed (packages/opencode/src/session/session.ts and
server/routes/instance/httpapi/groups/session.ts):

  GET {base_url}/session?limit={N}
    -> Session.Info[] (bare JSON array), sorted by time_updated DESC.
    Each Info has "id", "time": {"created": ms, "updated": ms}, and
    optional "model": {"providerID": ..., "id": ..., "variant": ...}.
    Server-side default limit is 100 if unspecified (session.ts
    listGlobal(): `.limit(input?.limit ?? 100)`) -- this script always
    passes an explicit limit to avoid depending on that default.
    time.updated is bumped by sessions.touch(), called from
    prompt.ts:1058 exactly once when a NEW message starts processing --
    NOT on every internal retry tick. A session stuck in opencode's own
    unbounded retry loop will NOT get a fresh time.updated during that
    retrying -- this is what actually lets the reaper catch it.

  DELETE {base_url}/session/{id}
    Confirmed (session.ts:608 remove()): calls cancelBackgroundJobs()
    first, which cancels any job with status "running" tied to that
    session's id/sessionId/parentSessionId, THEN deletes the session
    record. This is a real interrupt of in-flight work, not just a
    bookkeeping delete -- the reason this reaper uses DELETE rather
    than POST .../abort (which stops work but leaves the record, and
    would need a separate delete call to actually clean up).

UNVERIFIED: this has only been run against a mock HTTP server standing
in for opencode (see verify_session_reaper.py in the same delivery),
never a real opencode serve instance. The schema is source-confirmed;
the actual live behavior on the development host is not.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

ENABLED = os.environ.get("OPENCODE_REAPER_ENABLED", "true").lower() not in ("false", "0", "")
_PORT = os.environ.get("OPENCODE_SERVE_PORT", "4096")
BASE_URL = os.environ.get("OPENCODE_REAPER_BASE_URL", f"http://localhost:{_PORT}")
LOCAL_PROVIDER_KEY = os.environ.get("OPENCODE_OLLAMA_PROVIDER_KEY", "local/ollama")
# 10min -- aggressive, because sustained Ollama residency is the resource cost.
LOCAL_TTL_S = float(os.environ.get("OPENCODE_LOCAL_SESSION_TTL_S", "600"))
# 60min -- fallback for cloud and not-yet-chosen models. Stays above
# QUOTA_WAIT_THRESHOLD_S's 50min default so it never preempts a legitimate quota-retry wait
POLL_INTERVAL_S = float(os.environ.get("OPENCODE_REAPER_POLL_INTERVAL_S", "120"))
LIST_LIMIT = int(os.environ.get("OPENCODE_REAPER_LIST_LIMIT", "500"))


def log(msg: str) -> None:
    print(f"[session-reaper] {msg}", file=sys.stderr, flush=True)


def list_sessions(base_url: str, limit: int) -> list[dict]:
    url = f"{base_url.rstrip('/')}/session?limit={limit}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def delete_session(base_url: str, session_id: str) -> None:
    url = f"{base_url.rstrip('/')}/session/{session_id}"
    req = urllib.request.Request(url, method="DELETE")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def effective_ttl(info: dict, local_provider_key: str, local_ttl_s: float, default_ttl_s: float) -> float:
    """Which TTL applies to this session. Sessions whose model.providerID
    matches local_provider_key get the aggressive local TTL; everything
    else (other providers, or no model set yet) gets the generic default.
    """
    provider_id = (info.get("model") or {}).get("providerID")
    if provider_id == local_provider_key:
        return local_ttl_s
    return default_ttl_s


def reap_once(base_url: str, local_provider_key: str, local_ttl_s: float, default_ttl_s: float, limit: int) -> int:
    """Returns the number of sessions reaped this pass. Best-effort at
    every level -- a single session failing to list/delete never stops
    the rest of the pass or crashes the daemon loop.
    """
    try:
        sessions = list_sessions(base_url, limit)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        log(f"list_sessions failed this pass, skipping ({e})")
        return 0

    now_ms = time.time() * 1000
    reaped = 0
    for info in sessions:
        session_id = info.get("id")
        updated_ms = (info.get("time") or {}).get("updated")
        if not session_id or updated_ms is None:
            continue
        ttl_s = effective_ttl(info, local_provider_key, local_ttl_s, default_ttl_s)
        idle_s = (now_ms - updated_ms) / 1000
        if idle_s <= ttl_s:
            continue
        try:
            delete_session(base_url, session_id)
            provider_id = (info.get("model") or {}).get("providerID", "<unset>")
            log(f"reaped {session_id} (provider={provider_id}, idle {idle_s:.0f}s, TTL {ttl_s:.0f}s)")
            reaped += 1
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            log(f"delete failed for {session_id}, will retry next pass ({e})")
    return reaped


def main() -> int:
    if not ENABLED:
        log("OPENCODE_REAPER_ENABLED=false, exiting without starting the loop")
        return 0

    log(f"starting: base_url={BASE_URL} local_provider_key={LOCAL_PROVIDER_KEY!r} "
        f"local_ttl_s={LOCAL_TTL_S:.0f} default_ttl_s={TTL_S:.0f} "
        f"poll_interval_s={POLL_INTERVAL_S:.0f} list_limit={LIST_LIMIT}")
    while True:
        reap_once(BASE_URL, LOCAL_PROVIDER_KEY, LOCAL_TTL_S, TTL_S, LIST_LIMIT)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
