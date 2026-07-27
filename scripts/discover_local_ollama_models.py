#!/usr/bin/env python3
"""discover_local_ollama_models.py

Queries a running Ollama instance's native /api/tags endpoint (URL
sourced from opencode configuration via OPENCODE_OLLAMA_TAGS_URL, not
hardcoded) and merges the discovered model names into the
"local/ollama" provider's "models" map of a base opencode config,
writing the result to a new path.

Why this exists: opencode (released, not the still-open/unmerged
anomalyco/opencode#27554) does not auto-discover models for
@ai-sdk/openai-compatible providers -- every model ID must be listed
explicitly in config for opencode to route requests to it. This queries
Ollama directly at container startup, matching the "smart, auto-detect"
pattern Axiom already uses on the host side -- done here at the harness
level since opencode itself doesn't do it yet.

Source of truth as of the batch-4 migration (confirmed via opencode's
own source): your real global opencode config
(~/.config/opencode/opencode.json, via OPENCODE_GLOBAL_CONFIG) is the
actual source of truth for provider/model declarations, loaded by
opencode's own config.ts:loadGlobal() before anything else. This
script's live /api/tags query is an ADDITIVE layer on top, not a
replacement or fallback -- deep-merge semantics mean a model your global
config declares survives even if Ollama doesn't currently report it
(e.g. not pulled yet on this machine), while whatever Ollama actually
has loaded gets added automatically without needing manual config
edits. Confirmed intentional design, not a gap to fix.

Graceful degradation, not a hard dependency: if Ollama is unreachable
(offline, wrong URL, still starting up), this writes the base config
UNCHANGED rather than failing -- the base config's own
provider["local/ollama"]["models"] may be empty post-batch-4 (this
project no longer maintains a static list there), in which case your
global config -- merged in separately by opencode itself, upstream of
this script -- is what's actually relied on in that case. Mirrors the
"Errors are swallowed silently" design in the real upstream PR's
discovery mechanism.

Usage:
    python3 discover_local_ollama_models.py \\
        --base-config /opt/harness/opencode.base.json \\
        --ollama-tags-url http://host.docker.internal:11434/api/tags \\
        --output /home/harness/.config/opencode/opencode.runtime.json \\
        --provider-key local/ollama \\
        --timeout 3
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def fetch_ollama_model_names(tags_url: str, timeout: float) -> list[str]:
    """GET Ollama's native /api/tags and return the list of model names.

    Raises on any failure (timeout, connection refused, bad JSON,
    unexpected shape) -- caller decides what "failure" means (here:
    leave the base config's own local/ollama.models as-is, don't crash
    startup; your global config, merged in separately by opencode
    itself, is what's actually relied on for models in that case).
    """
    with urllib.request.urlopen(tags_url, timeout=timeout) as resp:
        body = json.loads(resp.read())
    models = body["models"]  # KeyError if Ollama's response shape changes
    names = [m["name"] for m in models]
    if not names:
        raise ValueError("Ollama /api/tags returned zero models")
    return names


def merge_models(base_config: dict, provider_key: str, model_names: list[str]) -> dict:
    """Return a new config dict with provider_key's "models" map merged
    with model_names (each newly-discovered name mapped to {}) -- a
    real union, not a replacement. A model declared in base_config but
    not currently reported by Ollama survives (e.g. installed but not
    loaded, or a transient discovery gap); anything Ollama reports gets
    added. Confirmed this needs to be a real union here, not just
    upstream where opencode's own config merge happens to compensate
    for a full replacement: a host-side caller reading this function's
    output file directly (rather than through opencode's own
    loadGlobal()-then-overlay merge) has no such compensating layer.

    Does not mutate base_config in place -- callers should still have
    the original available for comparison/logging if needed.
    """
    config = json.loads(json.dumps(base_config))  # cheap deep copy, stdlib-only
    if provider_key not in config.get("provider", {}):
        raise KeyError(
            f"provider {provider_key!r} not found in base config -- "
            "discovery has nothing to merge into"
        )
    existing_models = config["provider"][provider_key].get("models", {})
    merged_models = dict(existing_models)
    merged_models.update({name: {} for name in model_names})
    config["provider"][provider_key]["models"] = merged_models
    return config


def strip_jsonc_comments(text: str) -> str:
    """Real string-aware state machine, not a regex -- a naive `//.*`
    regex breaks on any string value containing "//", e.g. this same
    kind of config's own "https://opencode.ai/config.json" $schema
    field (hit that exact bug once already). Kept in sync with
    scripts/tools/read_local_ollama_models.py's identical copy -- not
    imported from there because this file is copied standalone into
    the minimal `server` container stage (no pip, no scripts/tools/
    alongside it there).
    """
    result = []
    i, n = 0, len(text)
    in_string = False
    escape = False
    while i < n:
        c = text[i]
        if in_string:
            result.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            result.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        result.append(c)
        i += 1
    return "".join(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-config", required=True, type=Path)
    parser.add_argument("--ollama-tags-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--provider-key", default="local/ollama")
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args(argv)

    base_config = json.loads(strip_jsonc_comments(args.base_config.read_text()))

    try:
        model_names = fetch_ollama_model_names(args.ollama_tags_url, args.timeout)
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
    ) as e:
        print(
            f"[discover_local_ollama_models] Ollama unreachable at "
            f"{args.ollama_tags_url} ({e.__class__.__name__}: {e}) -- "
            f"leaving {args.base_config}'s local/ollama.models unchanged "
            "(your global opencode config, merged in separately by "
            "opencode itself, is what's actually relied on for models "
            "in that case). This is expected if Ollama isn't running "
            "yet or OLLAMA_HOST isn't set to 0.0.0.0.",
            file=sys.stderr,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(base_config, indent=2))
        return 0  # not a fatal condition -- your global config still covers models

    merged = merge_models(base_config, args.provider_key, model_names)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(merged, indent=2))
    print(
        f"[discover_local_ollama_models] discovered {len(model_names)} "
        f"model(s) from {args.ollama_tags_url}: {', '.join(sorted(model_names))}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
