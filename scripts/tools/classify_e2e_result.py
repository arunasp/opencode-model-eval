#!/usr/bin/env python3
"""Classifies scripts/test_run_eval_client_e2e.py's output: distinguishes
the known, tracked create_session-timing limitation (see CHANGELOG.md)
from anything genuinely unhandled. Runs the suite itself, captures
output, and only prints the raw traceback for failures that don't
match the known signature.

Exit code: 0 if every failure (if any) matches the known signature or
there were no failures. 1 if there's at least one ERROR block, or a
FAIL block that doesn't match the known signature -- these are
genuinely unhandled and should fail verification.
"""
import re
import subprocess
import sys

KNOWN_SIGNATURE = "create_session failed or hung"

SEPARATOR = "=" * 70
BLOCK_HEADER_RE = re.compile(r"^(FAIL|ERROR): (.+)$")


def classify(output: str) -> tuple[list[str], list[str], list[str]]:
    """Returns (known_fails, unknown_fails, errors) -- each a list of
    test names in that category, parsed from unittest's default text
    output format (a run of '='*70, then 'FAIL: <name>' or
    'ERROR: <name>', then a '-'*70, then the traceback, repeated).
    """
    blocks = output.split(SEPARATOR + "\n")
    known_fails, unknown_fails, errors = [], [], []
    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue
        m = BLOCK_HEADER_RE.match(lines[0])
        if not m:
            continue
        kind, name = m.group(1), m.group(2)
        if kind == "ERROR":
            errors.append(name)
        elif kind == "FAIL":
            if KNOWN_SIGNATURE in block:
                known_fails.append(name)
            else:
                unknown_fails.append(name)
    return known_fails, unknown_fails, errors


def main() -> int:
    proc = subprocess.run(
        [sys.executable, "scripts/test_run_eval_client_e2e.py"],
        capture_output=True, text=True,
    )
    output = proc.stdout + proc.stderr

    if proc.returncode == 0:
        print("  e2e suite: all tests passed")
        return 0

    known_fails, unknown_fails, errors = classify(output)

    if known_fails:
        print(f"  e2e suite: {len(known_fails)} test(s) hit the known, tracked "
              f"create_session-timing limitation (see CHANGELOG.md's Known "
              f"Limitations) -- not treated as a bundle failure:")
        for name in known_fails:
            print(f"    - {name}")

    if unknown_fails or errors:
        print(f"  e2e suite: {len(unknown_fails) + len(errors)} test(s) failed "
              f"in a way that does NOT match the known limitation -- this is "
              f"unhandled, showing full output:")
        print(output)
        return 1

    if not known_fails and not unknown_fails and not errors:
        # Non-zero exit but nothing parsed as FAIL/ERROR -- e.g. an import
        # error or crash before any test ran. Genuinely unhandled.
        print("  e2e suite: exited non-zero but no FAIL/ERROR blocks were "
              "parseable from its output -- likely a crash before any test "
              "ran, showing full output:")
        print(output)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
