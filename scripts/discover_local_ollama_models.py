#!/usr/bin/env python3
"""discover_local_ollama_models.py

Queries a running Ollama instance's native /api/tags endpoint and merges
the discovered model names into the "local/ollama" provider's "models"
map of a base opencode config, writing the result to a new path.

Why this exists: opencode (released, not the still-open/unmerged
anomalyco/opencode#27554) does not auto-discover models for
@ai-sdk/openai-compatible providers -- every model ID must be listed
explicitly in config for opencode to route requests to it. Rather than
hand-maintain that list across three files (config/opencode.base.json,
docker-compose.yml, terraform/variables.tf) every time a model is
pulled or removed on the host, this queries Ollama directly at
container startup, matching the "smart, auto-detect" pattern Axiom
already uses on the host side -- done here at the harness level since
opencode itself doesn't do it yet.

Graceful degradation, not a hard dependency: if Ollama is unreachable
(offline, wrong URL, still starting up), this writes the base config
UNCHANGED rather than failing -- discovery is a nice-to-have on top of
the static fallback list already in config/opencode.base.json, not a
replacement for it. Mirrors the "Errors are swallowed silently" design
in the real upstream PR's discovery mechanism.

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
    fall back to the static list, don't crash startup).
    """
    with urllib.request.urlopen(tags_url, timeout=timeout) as resp:
        body = json.loads(resp.read())
    models = body["models"]  # KeyError if Ollama's response shape changes
    names = [m["name"] for m in models]
    if not names:
        raise ValueError("Ollama /api/tags returned zero models")
    return names


def merge_models(base_config: dict, provider_key: str, model_names: list[str]) -> dict:
    """Return a new config dict with provider_key's "models" map replaced
    by model_names (each mapped to {}, matching the existing schema).

    Does not mutate base_config in place -- callers should still have
    the original available for comparison/logging if needed.
    """
    config = json.loads(json.dumps(base_config))  # cheap deep copy, stdlib-only
    if provider_key not in config.get("provider", {}):
        raise KeyError(
            f"provider {provider_key!r} not found in base config -- "
            "discovery has nothing to merge into"
        )
    config["provider"][provider_key]["models"] = {name: {} for name in model_names}
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-config", required=True, type=Path)
    parser.add_argument("--ollama-tags-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--provider-key", default="local/ollama")
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args(argv)

    base_config = json.loads(args.base_config.read_text())

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
            "falling back to the static model list already in "
            f"{args.base_config}. This is expected if Ollama isn't "
            "running yet or OLLAMA_HOST isn't set to 0.0.0.0.",
            file=sys.stderr,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(base_config, indent=2))
        return 0  # not a fatal condition -- static fallback is valid

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
