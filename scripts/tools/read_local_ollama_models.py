#!/usr/bin/env python3
"""Reads provider["local/ollama"]["models"] from OPENCODE_GLOBAL_CONFIG
(or --config), tolerating JSONC comments -- the real file has them
(confirmed live: `// Merge into ~/.config/opencode/opencode.jsonc...`
etc.), and plain json.load() fails on it (confirmed live:
JSONDecodeError). Comment-stripping is a real state machine tracking
string context, not a regex -- a naive `//.*` regex breaks on any
string value containing "//", e.g. "https://opencode.ai/config.json"
in the file's own $schema field (hit this exact bug once already this
session).

Usage:
    python3 read_local_ollama_models.py                # newline-separated names
    python3 read_local_ollama_models.py --json          # [{"provider","model","full_id"}, ...]
    python3 read_local_ollama_models.py --config PATH   # override OPENCODE_GLOBAL_CONFIG
"""
import argparse
import json
import os
import sys


def strip_jsonc_comments(text: str) -> str:
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


def load_local_ollama_models(config_path: str) -> list[str]:
    raw = open(config_path, encoding="utf-8").read()
    data = json.loads(strip_jsonc_comments(raw))
    return list(data["provider"]["local/ollama"]["models"].keys())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("OPENCODE_GLOBAL_CONFIG"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.config:
        print("error: OPENCODE_GLOBAL_CONFIG not set and --config not given", file=sys.stderr)
        return 1

    models = load_local_ollama_models(args.config)
    if args.json:
        print(json.dumps([
            {"provider": "local/ollama", "model": m, "full_id": f"local/ollama/{m}"}
            for m in models
        ]))
    else:
        print("\n".join(models))
    return 0


if __name__ == "__main__":
    sys.exit(main())
