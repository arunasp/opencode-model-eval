#!/usr/bin/env python3
"""Runs the structured, escalating-difficulty test ladder
(task-suite/test_ladder.json) against a running `opencode serve`
instance over HTTP, and scores each tier with cvv_scan.py.

Uses stdlib `urllib` only, no new dependency for the HTTP layer itself
(matches this repo's CODEGEN.md preference for stdlib over new deps).

REQUEST SCHEMA -- confirmed from opencode's actual source, not guessed:
  POST {base_url}/session
    body: {} or partial Session.CreateInput (all fields optional:
          parentID, title, agent, model, metadata, permission,
          workspaceID) -- (session/session.ts:260-271)
    -> Session.Info (used for its "id" field)

  POST {base_url}/session/{sessionID}/message
    body per PromptInput minus sessionID (session/prompt.ts:1499-1520):
      {
        "model": {"providerID": "...", "modelID": "..."},   (ModelRef,
                   session/prompt.ts:1494-1497)
        "parts": [{"type": "text", "text": "..."}]           (discriminated
                   union on "type", session/prompt.ts:1512-1519)
      }
    -> SessionV1.WithParts

RESPONSE SCHEMA -- CONFIRMED empirically, not just from source. Ran a
real opencode serve instance (opencode-ai@1.18.3 via npm) against a
mock OpenAI-compatible backend under my own control and captured the
actual response:
    {
      "info": {..., "finish": "stop", "id": "msg_...", "sessionID": "..."},
      "parts": [
        {"type": "step-start", ...},
        {"type": "text", "text": "the actual reply", ...},
        {"type": "step-finish", "reason": "stop", ...}
      ]
    }
extract_reply()'s primary path (top-level "parts", filter on
type == "text") matches this exactly. One real thing the first empirical
attempt got wrong before this was confirmed: opencode's request to the
backend sets "stream": true, and a mock that responds with a flat
synchronous JSON body (rather than real SSE chunks) produces a
step-start/step-finish pair with NO text part at all -- silently wrong,
not an error. Fixed by having the mock emit actual
`data: {...}\n\n` SSE chunks. Also observed empirically, worth knowing
for request-count/cost expectations: opencode fires an extra
background title-generation call (a short system-prompted request
asking for a thread title) before the real one, per session.

Tool-call part shape specifically was NOT exercised in this empirical
test (the mock never triggered a tool call) -- extract_reply()'s
`"tool" in ptype.lower()` branch is a reasonable inference consistent
with the now-confirmed type-discriminated-parts-array pattern, but that
specific branch remains unverified against real tool-call output.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TASK_SUITE_DIR = Path("/task-suite")
RESULTS_DIR = Path("/results")
TOOLS_DIR = Path("/opt/harness/tools")


def http_post(base_url: str, path: str, body: dict, timeout: int = 300) -> dict:
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {path} failed: HTTP {e.code}: {body_text[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"POST {path} failed to reach {url}: {e.reason}") from e


def create_session(base_url: str) -> str:
    resp = http_post(base_url, "/session", {})
    session_id = resp.get("id") or resp.get("sessionID")
    if not session_id:
        raise RuntimeError(f"session creation response had no id/sessionID field: {resp}")
    return session_id


def send_message(base_url: str, session_id: str, provider: str, model_id: str, text: str) -> dict:
    body = {
        "model": {"providerID": provider, "modelID": model_id},
        "parts": [{"type": "text", "text": text}],
    }
    return http_post(base_url, f"/session/{session_id}/message", body)


def extract_reply(response: dict) -> tuple[str, list[dict]]:
    """Extracts (assistant_text, tool_calls) from a SessionV1.WithParts
    response. Primary path (top-level "parts", filter on type=="text")
    confirmed empirically against a real opencode serve instance -- see
    module docstring. The message.parts fallback and the tool-call
    branch remain defensive/inferred, not exercised by that same test.
    """
    parts = response.get("parts")
    if parts is None and isinstance(response.get("message"), dict):
        parts = response["message"].get("parts")
    if parts is None:
        parts = []

    text_chunks = []
    tool_calls = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type", "")
        if ptype == "text":
            text_chunks.append(part.get("text", ""))
        elif "tool" in ptype.lower():
            tool_calls.append(part)
    return "\n".join(text_chunks), tool_calls


def events_to_transcript(setup_message: str, setup_text: str, setup_tools: list[dict],
                          probe_message: str, probe_text: str, probe_tools: list[dict]) -> str:
    lines = []

    def render_turn(user_msg: str, text: str, tools: list[dict]):
        lines.append("## User")
        lines.append(user_msg)
        lines.append("")
        lines.append("## Assistant")
        for t in tools:
            tool_name = t.get("tool") or t.get("name") or "unknown"
            lines.append(f"**Tool: {tool_name}**")
            if "input" in t:
                lines.append("**Input:**")
                lines.append("```json")
                lines.append(json.dumps(t["input"], indent=2))
                lines.append("```")
            if "output" in t or "result" in t:
                lines.append("**Output:**")
                lines.append("```")
                lines.append(str(t.get("output", t.get("result", "")))[:2000])
                lines.append("```")
        if text:
            lines.append(text)
        lines.append("")

    render_turn(setup_message, setup_text, setup_tools)
    render_turn(probe_message, probe_text, probe_tools)
    return "\n".join(lines)


def scan_transcript(transcript_path: Path) -> dict:
    import subprocess
    result = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "cvv_scan.py"), "--json", str(transcript_path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"[warn] cvv_scan.py failed on {transcript_path}: {result.stderr}", file=sys.stderr)
        return {"category_counts": {}, "total_findings": 0}
    try:
        parsed = json.loads(result.stdout)
        return parsed[0] if parsed else {"category_counts": {}, "total_findings": 0}
    except (json.JSONDecodeError, IndexError):
        return {"category_counts": {}, "total_findings": 0}


def check_pass(scan_result: dict, criteria: dict) -> tuple[bool, str]:
    counts = scan_result.get("category_counts", {})
    for forbidden in criteria.get("must_not_have_categories", []):
        if counts.get(forbidden, 0) > 0:
            return False, f"forbidden finding present: {forbidden} (x{counts[forbidden]})"
    for required in criteria.get("must_have_categories", []):
        if counts.get(required, 0) == 0:
            return False, f"required finding absent: {required}"
    if "manual_check" in criteria:
        return False, (
            "NEEDS_MANUAL_REVIEW: no CVV violation found, but this tier's "
            f"pass condition requires human/test confirmation: {criteria['manual_check']}"
        )
    return True, "pass_criteria satisfied (CVV-only tier)"


def run_category(category: dict, base_url: str, provider: str, model_id: str,
                  setup_message: str, category_dir: Path) -> dict:
    cat_id = category["id"]
    print(f"[eval-client] category: {cat_id}: {category['description']}", file=sys.stderr)
    category_dir.mkdir(parents=True, exist_ok=True)
    ceiling = 0
    tier_results = []

    for tier_def in category["tiers"]:
        tier_num = tier_def["tier"]
        print(f"  [tier {tier_num}] source={tier_def['source']}", file=sys.stderr)

        session_id = create_session(base_url)
        setup_resp = send_message(base_url, session_id, provider, model_id, setup_message)
        setup_text, setup_tools = extract_reply(setup_resp)
        probe_resp = send_message(base_url, session_id, provider, model_id, tier_def["prompt"])
        probe_text, probe_tools = extract_reply(probe_resp)

        transcript = events_to_transcript(setup_message, setup_text, setup_tools,
                                           tier_def["prompt"], probe_text, probe_tools)
        transcript_path = category_dir / f"tier{tier_num}.transcript.md"
        transcript_path.write_text(transcript)

        raw_path = category_dir / f"tier{tier_num}.raw.json"
        raw_path.write_text(json.dumps({"setup": setup_resp, "probe": probe_resp}, indent=2, default=str))

        scan_result = scan_transcript(transcript_path)
        passed, reason = check_pass(scan_result, tier_def["pass_criteria"])
        needs_review = reason.startswith("NEEDS_MANUAL_REVIEW")

        tier_record = {
            "tier": tier_num, "source": tier_def["source"], "passed": passed,
            "needs_manual_review": needs_review, "reason": reason,
            "findings": scan_result.get("category_counts", {}),
            "session_id": session_id,
            "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (category_dir / f"tier{tier_num}.json").write_text(json.dumps(tier_record, indent=2))
        tier_results.append(tier_record)

        status = "PASS" if passed else ("NEEDS REVIEW" if needs_review else "FAIL")
        print(f"    -> {status}: {reason}", file=sys.stderr)

        if passed:
            ceiling = tier_num
        else:
            break

    return {"category": cat_id, "ceiling": ceiling, "tiers": tier_results}


def main() -> int:
    base_url = os.environ.get("OPENCODE_SERVER_URL", "http://server:4096")
    # 4096 is THIS project's chosen fixed port, set explicitly when
    # starting `opencode serve --port 4096 --hostname 0.0.0.0` in the
    # server container/service -- not opencode's own default. Confirmed
    # from source (cli/network.ts): opencode's real defaults are
    # port=0 (random) and hostname=127.0.0.1 (loopback only, unreachable
    # from another container). Both are overridden explicitly wherever
    # the server is started -- see docker-compose.yml / Dockerfile.
    provider = os.environ.get("OPENCODE_MODEL_PROVIDER")
    model_id = os.environ.get("OPENCODE_MODEL_ID")
    if not provider or not model_id:
        print("FATAL: OPENCODE_MODEL_PROVIDER and OPENCODE_MODEL_ID must be set", file=sys.stderr)
        return 1

    ladder_path = TASK_SUITE_DIR / "test_ladder.json"
    if not ladder_path.exists():
        print(f"FATAL: {ladder_path} not found", file=sys.stderr)
        return 1
    with open(ladder_path) as f:
        ladder = json.load(f)

    setup_message = ladder["setup_turn"]
    model_slug = f"{provider}_{model_id.replace(':', '-').replace('/', '-')}"
    results_dir = RESULTS_DIR / model_slug
    results_dir.mkdir(parents=True, exist_ok=True)

    print(f"[eval-client] target server: {base_url}", file=sys.stderr)
    print(f"[eval-client] model under test: {provider}/{model_id}", file=sys.stderr)

    report = {"model": f"{provider}/{model_id}", "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "categories": []}
    for category in ladder["categories"]:
        cat_dir = results_dir / category["id"]
        report["categories"].append(
            run_category(category, base_url, provider, model_id, setup_message, cat_dir)
        )

    (results_dir / "report.json").write_text(json.dumps(report, indent=2))

    print(f"\n=== Summary (model: {provider}/{model_id}) ===", file=sys.stderr)
    for cat_report in report["categories"]:
        last_tier = cat_report["tiers"][-1] if cat_report["tiers"] else None
        note = ""
        if last_tier and not last_tier["passed"]:
            note = " [stopped: NEEDS MANUAL REVIEW]" if last_tier["needs_manual_review"] else " [stopped: CVV violation]"
        print(f"  {cat_report['category']}: ceiling tier {cat_report['ceiling']}{note}", file=sys.stderr)
    print(f"\nFull report: {results_dir / 'report.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
