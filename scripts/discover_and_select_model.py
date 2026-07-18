#!/usr/bin/env python3
"""Discover available models from opencode's real provider pool, or
select a specific provider/model directly.

Two modes:
  --model provider/id   Skip discovery entirely, use this model. Primary
                         path for reproducible runs -- the existing named
                         compose services (hy3, deepseek-v4-pro, glm-5-2,
                         gemma4-31b-local) already do this at build time;
                         this flag is for ad-hoc runs against something
                         not in that fixed list.
  (no --model)           Query `opencode models --verbose`, filter to
                         free/available candidates, exclude anything
                         passed via --exclude, and select one.

Uses `opencode models --verbose`, confirmed from opencode's CLI source
(packages/opencode/src/cli/cmd/models.ts) to print, per model:
    <providerID>/<modelID>
    { ...model metadata JSON, including an optional "cost" field ... }

Free-tier heuristic (cost field absent, or all cost sub-values are 0)
is inferred from the confirmed schema
(packages/core/src/models-dev.ts: `cost: Schema.optional(Cost)`), but
has not been spot-checked against real provider output -- if selection
looks wrong, run `opencode models --verbose` directly and inspect the
actual "cost" shape for the providers involved before trusting this
filter blindly.

Output: writes OPENCODE_MODEL_PROVIDER=... / OPENCODE_MODEL_ID=... to
/results/discovered-model.env (consumable by a subsequent
`docker compose run --env-file` invocation) and also prints the
selected provider/id to stdout.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


def run_opencode_models_verbose() -> str:
    result = subprocess.run(
        ["opencode", "models", "--verbose"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"ERROR: 'opencode models --verbose' failed (exit {result.returncode}): "
              f"{result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def parse_models(raw: str) -> list[dict]:
    """Parse the `providerID/modelID\\n{json}\\n` stream into records.
    Tolerant of the JSON block spanning multiple lines (pretty-printed).
    """
    lines = raw.splitlines()
    records = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "/" in line and not line.startswith("{"):
            provider_model = line
            i += 1
            json_lines = []
            depth = 0
            started = False
            while i < len(lines):
                json_lines.append(lines[i])
                depth += lines[i].count("{") - lines[i].count("}")
                if "{" in lines[i]:
                    started = True
                i += 1
                if started and depth <= 0:
                    break
            try:
                meta = json.loads("\n".join(json_lines)) if json_lines else {}
            except json.JSONDecodeError:
                meta = {}
            provider_id, _, model_id = provider_model.partition("/")
            records.append({
                "provider": provider_id, "model": model_id,
                "full_id": provider_model, "meta": meta,
            })
        else:
            i += 1
    return records


def is_free(record: dict) -> bool:
    cost = record.get("meta", {}).get("cost")
    if cost is None:
        return True
    if isinstance(cost, dict):
        return all(float(v) == 0 for v in cost.values() if isinstance(v, (int, float)))
    return False


def select_candidate(records: list[dict], exclude_ids: set[str]) -> dict | None:
    free_candidates = [
        r for r in records
        if is_free(r) and r["full_id"] not in exclude_ids and r["provider"] != "opencode"
    ]
    if not free_candidates:
        return None

    def size_hint(r: dict) -> int:
        mid = r["model"].lower()
        for kw, score in [("70b", 5), ("large", 4), ("pro", 3), ("v4", 3),
                          ("v3", 2), ("mini", -1), ("small", -1), ("nano", -2)]:
            if kw in mid:
                return score
        return 0

    free_candidates.sort(key=size_hint, reverse=True)
    return free_candidates[0]


def write_env_file(provider: str, model_id: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"OPENCODE_MODEL_PROVIDER={provider}\nOPENCODE_MODEL_ID={model_id}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--model", metavar="provider/id",
        help="Skip discovery, select this exact provider/model directly.",
    )
    parser.add_argument(
        "--exclude", action="append", default=[],
        help="provider/model to exclude from discovery (e.g. a model already under separate test). Repeatable.",
    )
    parser.add_argument(
        "--out", default="/results/discovered-model.env",
        help="path to write the resolved OPENCODE_MODEL_PROVIDER/ID env file",
    )
    args = parser.parse_args()

    if args.model:
        if "/" not in args.model:
            print(f"ERROR: --model must be provider/id, got: {args.model}", file=sys.stderr)
            return 1
        provider, _, model_id = args.model.partition("/")
        write_env_file(provider, model_id, Path(args.out))
        print(args.model)
        print(f"Selected directly (no discovery): {args.model}", file=sys.stderr)
        return 0

    exclude_ids = set(args.exclude)
    raw = run_opencode_models_verbose()
    records = parse_models(raw)
    if not records:
        print("ERROR: no models parsed from 'opencode models --verbose' output. "
              "CLI output format may have changed -- inspect raw output.", file=sys.stderr)
        return 1

    selected = select_candidate(records, exclude_ids)
    if selected is None:
        print("ERROR: no free, non-excluded candidate found.", file=sys.stderr)
        print(f"Total models seen: {len(records)}. Excluded: {sorted(exclude_ids)}", file=sys.stderr)
        return 1

    write_env_file(selected["provider"], selected["model"], Path(args.out))
    print(selected["full_id"])
    print(f"Selected via discovery: {selected['full_id']} (from {len(records)} candidates, "
          f"{len(exclude_ids)} excluded)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
