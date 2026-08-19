# Requirements

Everything here was verified against actual repo content (shebangs,
`command -v` checks already in the scripts, Dockerfile package lists,
`terraform/versions.tf`), not assumed. Run `bash scripts/check-requirements.sh`
to check your own machine against this list automatically.

## Always required (host machine)

| Dependency | Why | Notes |
|---|---|---|
| **Docker Engine** | Everything runs in containers -- server, eval, discover, git-workspace, jupyter | |
| **Docker Compose** | Primary orchestration path | Either the v2 plugin (`docker compose`) or the legacy standalone binary (`docker-compose`) works. Every call site goes through `scripts/lib/compose.sh`, which picks whichever is on PATH and fails with a named error when neither is; `scripts/compose.sh` is the same resolution for the Makefile. The legacy CLI has no precondition/pre-flight hook mechanism (relevant to `scripts/ensure-auth-data.sh`, see INSTALL.md) |
| **bash** | `harness-control.sh`, `scripts/select-and-run-eval.sh`, `scripts/extract-opencode-key.sh`, `scripts/ensure-auth-data.sh`, `scripts/tf-select-and-run-eval.sh`, `scripts/tf-extract-auth-keys.sh`, `scripts/ollama-model-switch.sh` all shebang `#!/bin/bash` or `#!/usr/bin/env bash` | POSIX `sh` alone is NOT enough for these -- `entrypoint.sh` is the one script that deliberately stays POSIX `sh` (runs inside the container, which has no bash installed by design) |
| **jq** | Hard requirement (no fallback) in `scripts/extract-opencode-key.sh`, `scripts/tf-extract-auth-keys.sh`, `scripts/ollama-model-switch.sh`; soft dependency (falls back to `less`) in `harness-control.sh`'s results browser | Setup (credential scoping) needs this on every path, Compose or Terraform |
| **Python 3.10+** | `discover_and_select_model.py` and others use `X \| None` / `list[dict]` union-type syntax, which needs 3.10+ | Only matters for scripts you run directly on the host (most Python runs inside containers, which get their own pinned `python3` from the base image) |
| **A real opencode global config**, `~/.config/opencode/opencode.json` | Every container that runs opencode (`server`, `discover`, `eval`, `git-workspace`) mounts this read-only and merges it under this project's own config. Your own `provider["local/ollama"]["models"]` declarations are what makes a given Ollama model resolvable at all -- this project no longer maintains its own static copy of that list. **Format required**: a `provider["local/ollama"]` entry with a `models` object listing every local model you want reachable, e.g. `{"models": {"qwen2.5-coder:7b": {}, "NitrAI/VibeThinker-3B:latest": {}}}`. Confirmed via source (`config.ts`'s `loadGlobal()`) that this loads *before* the project's own `OPENCODE_CONFIG` -- an overlay, not a replacement, so your global permissions/baseURL settings on this provider are safely overridden by the project's own (`edit`/`bash: deny` for locked-down roles, the container-correct `baseURL` via `OPENCODE_OLLAMA_BASE_URL`) regardless of what your global file sets |
| **`OPENCODE_GLOBAL_CONFIG`** env var, set to the absolute path of the file above | Must be exported by your shell before running `docker-compose`/`terraform` -- e.g. `export OPENCODE_GLOBAL_CONFIG="$HOME/.config/opencode/opencode.json"`. Deliberately not defaulted to a `~`-prefixed path in `docker-compose.yml`: confirmed via real `docker/compose` issues (#6506, #3872) that tilde expansion in a volume host path is inconsistent across compose versions and container-vs-native installs. The Terraform path uses `var.opencode_global_config_path` (same default, expanded via Terraform's own `pathexpand()`, which -- unlike Compose -- is documented, reliable behavior) |

## Required for the primary entry point (`harness-control.sh`)

| Dependency | Why |
|---|---|
| **tmux** | Hard requirement, checked explicitly (`harness-control.sh` exits with a clear error if missing) -- the whole UI is a tmux split-pane session. Run it directly from a real terminal, not piped or from CI |
| A real terminal | Host-side arrow-key/j-k model picker (`scripts/lib/host-model-picker.sh`) needs a TTY; without one, discovery falls back to an unattended auto-pick instead |

## Required for the Terraform path

| Dependency | Why |
|---|---|
| **Terraform >= 1.1.5** | `terraform/versions.tf`'s `required_version` |
| `kreuzwerker/docker` provider `~> 4.5` | Manages all Docker resources declaratively |
| `hashicorp/external` provider `~> 2.3` | `data.external.auth_keys` -- automatic credential extraction on `plan`/`apply` |
| `hashicorp/null` provider `~> 3.2` | `null_resource` + `local-exec` for jupyter's connect-URL printing (deliberately not `data "external"`, which would leak into tfstate) |

Providers are pulled automatically by `terraform init` -- nothing to install by hand beyond the `terraform` binary itself.

## Required for local Ollama models

| Dependency | Why |
|---|---|
| **Ollama**, running on the host | The `server` container reaches it via `host.docker.internal:host-gateway` |
| Ollama started with `OLLAMA_HOST=0.0.0.0:11434` | Its default bind (`127.0.0.1` loopback-only) is NOT reachable from inside a container -- this is a real, confirmed gotcha, not a hypothetical one |
| **curl** | `scripts/ollama-model-switch.sh` (host-side load/unload control) |

## Required at Docker build time

| Dependency | Why |
|---|---|
| Network access to `github.com` | `scripts/fetch_embedding_model.sh` clones a GitHub repo with ONNX embedding weights committed in-repo (deliberately avoids a Hugging Face/Ollama runtime dependency). Only matters for the best-case onnxruntime-backed semantic path -- harmless if it fails, `axiom_cvv_verify.py` falls through to its own TF-IDF implementation either way |
| Network access to the standard `apk`/`pip` package sources | Dockerfile's `server` stage: `ca-certificates`, `python3`. `harness` stage adds: `py3-pip`, `git`, then `pip install spacy click`, `pip install onnxruntime tokenizers numpy` (`axiom_cvv_verify.py` tries this first for its semantic action-detection, falls through to a TF-IDF implementation needing nothing beyond numpy if this genuinely isn't available -- expected on Alpine/musllinux and/or Python 3.14, confirmed no published wheel for either). `jupyter` stage adds `pip install jupyterlab` |

None of these are host-side installs -- they happen inside the Docker build, listed here so a build failure in an offline/restricted environment points at the right cause.

## Optional (development/testing only)

| Dependency | Why | Notes |
|---|---|---|
| **node + npm** | `scripts/test_run_eval_client_e2e.py` installs and runs the real `opencode-ai` npm package as part of its own real end-to-end test | Not needed for normal use -- only for running that specific test. Skips itself with a clear message if node/npm aren't on PATH |
| Network access to the npm registry (`registry.npmjs.org`) | Same test, to install `opencode-ai` | |
| Network access to whatever domain `opencode serve`'s own startup path reaches out to | Same test -- unconfirmed which domain, still an open investigation (see CHANGELOG.md) | If this test hangs specifically at `POST /session` with the server otherwise listening, this is why |
| **numpy** (host-side, only if running `scripts/tools/test_axiom_cvv_action_detection.py` directly outside Docker) | That test imports `axiom_cvv_verify.py`, which imports numpy unconditionally for its TF-IDF fallback (and the onnxruntime path, when available) | Guaranteed present inside the actual Docker image (a transitive dependency of spaCy, installed at build time) -- this only matters if you want to run this one test file directly on your host/local venv rather than inside a container. `pip install numpy` into whatever venv you use for that. Patch-bundle verification scripts skip this specific test with a clear message if numpy isn't importable on the host, rather than treating it as a hard failure |
| **shellcheck** | Dev-side bash linting, referenced in `docs/BRANCHING.md`'s verification step and this project's own patch-bundle scripts | Not required to run the harness itself |
| **terraform** binary specifically (as opposed to just HCL2-parseable files) | A real `terraform validate`/`plan`/`apply` | Patch bundles fall back to a brace-balance sanity check if this isn't installed, which is explicitly NOT a substitute |

## Not required

- **Node/npm for normal use** -- only the optional e2e test needs it; the actual harness (`run_eval_client.py`, `discover_and_select_model.py`, etc.) is Python stdlib only, deliberately (see `docs/CODEGEN.md`)
- **A Hugging Face account/token** -- the embedding model is fetched from a plain GitHub clone, not the HF Hub
- **Ollama** -- only if you're testing local models; cloud-provider evals need nothing beyond your `auth.json`
