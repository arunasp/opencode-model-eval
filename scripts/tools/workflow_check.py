#!/usr/bin/env python3
"""Check GitHub Actions workflows parse, and that embedded heredocs
terminate where the shell expects.

WHY THIS IS A CHECK. A malformed workflow does not fail loudly -- GitHub
declines to run it, so the repository simply stops having CI and the
next push looks the same as the last one. That is the same shape as a
skipped stage reporting a pass, and it deserves the same treatment: a
check on the write path rather than a habit of remembering to look.

WHAT IT CATCHES BEYOND `yaml.safe_load`:

1. A heredoc whose terminator is not at column 0. `<<'EOF'` (without the
   dash) requires the closing word to start at the beginning of the
   line. YAML block scalars strip a common indent, so whether that holds
   depends on how the block was written -- and an editor or connector
   that re-indents a block silently breaks it. This repo has been bitten
   by exactly that re-indentation twice: once with Python embedded in
   `tools/pipeline.sh`, where the resulting IndentationError went to
   /dev/null and left a stage unable to pass anywhere, and once in this
   workflow file. The failure mode is an unterminated heredoc that
   swallows the rest of the script.

2. A `<<'PY'`-style Python heredoc whose body does not compile. Same
   class as the pipeline bug: the error appears at run time, inside a
   job, long after the change that caused it.

Requires PyYAML, which lives in requirements-dev.txt and therefore in
the project-local .venv. Exits 2 (SKIPPED, per tools/pipeline.sh's
convention) when it is absent, naming what to run.

Usage:
    python3 scripts/tools/workflow_check.py [path ...]
    (defaults to .github/workflows/*.yml and *.yaml)
"""
from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

# `<<-` strips leading TABS from the terminator, so it is exempt from
# the column-0 rule. Quoted and unquoted delimiters both matter here.
HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def check_heredocs(where: str, script: str) -> list[str]:
    """Return a problem per heredoc that will not behave as written."""
    problems = []
    lines = script.splitlines()
    for i, line in enumerate(lines):
        match = HEREDOC.search(line)
        if not match:
            continue
        dash, _quote, word = match.groups()
        terminator = None
        for j in range(i + 1, len(lines)):
            if lines[j].strip() == word:
                terminator = j
                break
        if terminator is None:
            problems.append(f"{where}: heredoc <<{word} is never terminated")
            continue
        if not dash and lines[terminator] != word:
            problems.append(
                f"{where}: heredoc <<{word} terminator is indented "
                f"({lines[terminator]!r}); without <<- it must start at "
                f"column 0, or the heredoc swallows the rest of the script"
            )
            continue
        # A Python heredoc that does not compile fails inside the job.
        if word.upper().startswith("PY"):
            body = "\n".join(lines[i + 1:terminator])
            try:
                compile(body, f"{where}:<<{word}", "exec")
            except SyntaxError as exc:
                problems.append(
                    f"{where}: python heredoc <<{word} does not compile: {exc}"
                )
    return problems


def check_workflow(path: Path, yaml) -> list[str]:
    problems = []
    try:
        workflow = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        return [f"{path}: does not parse: {exc}"]
    if not isinstance(workflow, dict) or "jobs" not in workflow:
        return [f"{path}: no 'jobs' key -- GitHub will not run this"]

    for job_name, spec in workflow["jobs"].items():
        for n, step in enumerate(spec.get("steps", []), 1):
            script = step.get("run")
            if not script:
                continue
            label = step.get("name") or f"step {n}"
            problems += check_heredocs(f"{path}: {job_name}: {label}", script)
    return problems


def main(argv):
    try:
        import yaml
    except ImportError:
        print("PyYAML not installed -- run 'make deps'; skipping workflow check")
        return 2

    paths = [Path(a) for a in argv] or [
        Path(p) for pattern in (".github/workflows/*.yml", ".github/workflows/*.yaml")
        for p in sorted(glob.glob(pattern))
    ]
    if not paths:
        print("no workflows to check")
        return 0

    problems = []
    for path in paths:
        problems += check_workflow(path, yaml)

    for problem in problems:
        print(f"  {problem}")
    print(f"workflows: {len(paths)} checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
