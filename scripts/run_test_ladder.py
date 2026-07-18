#!/usr/bin/env python3
"""Runs the structured, escalating-difficulty test ladder
(task-suite/test_ladder.json) against the model configured via
OPENCODE_MODEL_PROVIDER / OPENCODE_MODEL_ID, and scores each tier with
cvv_scan.py.

Replaces the old flat task-suite/prompts/*.txt, one-shot-per-file design.
Structured results land under /results/<category>/tier<N>.* instead of a
single flat manifest -- each tier gets its own transcript, scan output,
and pass/fail record, plus a top-level report.json summarizing ceilings.

Uses `opencode run <message> --model <provider>/<id> --format json
[--session <id> --continue]`, confirmed real flags from opencode's CLI
source (packages/opencode/src/cli/cmd/run.ts).

KNOWN UNVERIFIED INTEGRATION POINT, disclosed rather than hidden: the
exact --format json event schema has not been inspected against real
output. events_to_transcript() below uses a best-effort field mapping.
If a run produces transcripts cvv_scan.py can't parse, inspect a raw
--format json sample and fix the mapping here first, before assuming
the scoring logic itself is broken.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

TASK_SUITE_DIR = Path("/task-suite")
RESULTS_DIR = Path("/results")
TOOLS_DIR = Path("/opt/harness/tools")


def run_opencode_turn(message: str, model: str, session_id: str | None) -> tuple[dict, str | None]:
    cmd = ["opencode", "run", message, "--model", model, "--format", "json"]
    if session_id:
        cmd += ["--session", session_id, "--continue"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    events = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"_unparsed": line})
    new_session_id = session_id
    for e in events:
        if not isinstance(e, dict):
            continue
        for key in ("sessionID", "session_id", "sessionId"):
            if key in e:
                new_session_id = e[key]
                break
    return {"events": events, "returncode": result.returncode, "stderr": result.stderr}, new_session_id


def events_to_transcript(setup_message: str, setup_result: dict,
                          probe_message: str, probe_result: dict) -> str:
    lines = []

    def render_turn(user_msg: str, result: dict):
        lines.append("## User")
        lines.append(user_msg)
        lines.append("")
        lines.append("## Assistant")
        for e in result["events"]:
            if not isinstance(e, dict) or "_unparsed" in e:
                continue
            etype = e.get("type", "")
            if "tool" in etype.lower():
                tool_name = e.get("tool") or e.get("name") or "unknown"
                lines.append(f"**Tool: {tool_name}**")
                if "input" in e:
                    lines.append("**Input:**")
                    lines.append("```json")
                    lines.append(json.dumps(e["input"], indent=2))
                    lines.append("```")
                if "output" in e or "result" in e:
                    lines.append("**Output:**")
                    lines.append("```")
                    lines.append(str(e.get("output", e.get("result", "")))[:2000])
                    lines.append("```")
            elif etype in ("text", "message", "assistant_text") or "text" in e:
                text = e.get("text") or e.get("content") or ""
                if text:
                    lines.append(str(text))
        if result["returncode"] != 0:
            lines.append(f"\n[non-zero exit: {result['returncode']}, stderr: {result['stderr'][:500]}]")
        lines.append("")

    render_turn(setup_message, setup_result)
    render_turn(probe_message, probe_result)
    return "\n".join(lines)


def scan_transcript(transcript_path: Path) -> dict:
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
    """See axiom_cvv_verify.py's check_pass for the reasoning behind this
    -- a manual_check field can only rule out an automatic FAIL on CVV
    grounds, it cannot confirm a PASS by itself.
    """
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


def run_category(category: dict, model: str, setup_message: str, category_dir: Path) -> dict:
    cat_id = category["id"]
    print(f"[test-ladder] category: {cat_id}: {category['description']}", file=sys.stderr)
    category_dir.mkdir(parents=True, exist_ok=True)
    ceiling = 0
    tier_results = []

    for tier_def in category["tiers"]:
        tier_num = tier_def["tier"]
        print(f"  [tier {tier_num}] source={tier_def['source']}", file=sys.stderr)

        setup_result, session_id = run_opencode_turn(setup_message, model, None)
        probe_result, _ = run_opencode_turn(tier_def["prompt"], model, session_id)

        transcript = events_to_transcript(setup_message, setup_result, tier_def["prompt"], probe_result)
        transcript_path = category_dir / f"tier{tier_num}.transcript.md"
        transcript_path.write_text(transcript)

        raw_path = category_dir / f"tier{tier_num}.raw.json"
        raw_path.write_text(json.dumps({"setup": setup_result, "probe": probe_result}, indent=2, default=str))

        scan_result = scan_transcript(transcript_path)
        passed, reason = check_pass(scan_result, tier_def["pass_criteria"])
        needs_review = reason.startswith("NEEDS_MANUAL_REVIEW")

        tier_record = {
            "tier": tier_num, "source": tier_def["source"], "passed": passed,
            "needs_manual_review": needs_review, "reason": reason,
            "findings": scan_result.get("category_counts", {}),
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
    provider = os.environ.get("OPENCODE_MODEL_PROVIDER")
    model_id = os.environ.get("OPENCODE_MODEL_ID")
    if not provider or not model_id:
        print("FATAL: OPENCODE_MODEL_PROVIDER and OPENCODE_MODEL_ID must be set", file=sys.stderr)
        return 1
    model = f"{provider}/{model_id}"

    ladder_path = TASK_SUITE_DIR / "test_ladder.json"
    if not ladder_path.exists():
        print(f"FATAL: {ladder_path} not found", file=sys.stderr)
        return 1
    with open(ladder_path) as f:
        ladder = json.load(f)

    setup_message = ladder["setup_turn"]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    report = {"model": model, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "categories": []}
    for category in ladder["categories"]:
        cat_dir = RESULTS_DIR / category["id"]
        report["categories"].append(run_category(category, model, setup_message, cat_dir))

    (RESULTS_DIR / "report.json").write_text(json.dumps(report, indent=2))

    print(f"\n=== Summary (model: {model}) ===", file=sys.stderr)
    for cat_report in report["categories"]:
        last_tier = cat_report["tiers"][-1] if cat_report["tiers"] else None
        note = ""
        if last_tier and not last_tier["passed"]:
            note = " [stopped: NEEDS MANUAL REVIEW]" if last_tier["needs_manual_review"] else " [stopped: CVV violation]"
        print(f"  {cat_report['category']}: ceiling tier {cat_report['ceiling']}{note}", file=sys.stderr)
    print(f"\nFull report: {RESULTS_DIR / 'report.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
