# Contributing

The maintainer commits directly to `main`; outside contributions go
through a pull request. See `docs/BRANCHING.md` for branch naming.

## Before you start

Open an issue before writing code for anything beyond a trivial fix.
Bug report and feature request templates are provided.

## Making a change

1. Fork, branch off a freshly fetched `main`.
2. Prefix the branch `feat/`, `fix/`, `docs/` or `chore/` — see
   `docs/BRANCHING.md`.
3. Keep the change focused. One PR, one concern.
4. Verify what you touched:
   - Bash — `shellcheck`
   - Terraform — `terraform validate`, or the HCL2 parser if the binary
     is unavailable
   - Docker — `docker compose build` / `docker build`
   - Behaviour-visible changes (`harness-control.sh`, the pickers, the
     eval flow) — state what you ran and what you observed. A syntax
     check is not a substitute.
5. Open the PR against `main`.

`make ci` runs lint, prose, test, verify, e2e and client. Stages skip
rather than fail when a dependency is absent, and report the reason.

`make deps` installs the dev dependencies (`requirements-dev.txt`) into
a project-local `.venv`; `make lint` does it for you.

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and every pull
request, in four jobs:

| Job | Runs | Covers |
|---|---|---|
| `checks` | `lint`, `prose`, `verify` | no Node or provider needed, so it reports in seconds |
| `test` | `make test` | includes the e2e suite, which starts `opencode serve` as a subprocess |
| `e2e-mock` | `make e2e` | model discovery |
| `containers-mock` | `make containers` | the compose stack and `scripts/e2e_session_probe.py`, which nothing else exercises |

It calls the same `make` targets you run locally. A workflow that grows
its own command sequence drifts from the Makefile, and then each becomes
an excuse for the other's failure.

**No model is called.** `test` starts an `opencode serve` process and
drives it against `scripts/tools/mock_openai_backend.py`; `e2e-mock`
points discovery at that same mock's `/api/tags`; `containers-mock` runs
the containerised server against it through `host.docker.internal`.
Nothing reaches Ollama, a cloud provider, or a credential, so a fork's
CI works with no secrets configured.

Both mock-backed jobs assert their stage did not SKIP. A skipped stage
exits 0, so without that a job goes green when the mock dies or Docker
detection breaks, reporting a skip as a pass.

`make lint` parses the workflows themselves
(`scripts/tools/workflow_check.py`). A malformed workflow does not fail
loudly: GitHub declines to run it and the repository quietly stops
having CI. The check also verifies that heredocs inside `run:` blocks
terminate at column 0 and that Python heredoc bodies compile, both of
which otherwise fail inside a job.

Two environment differences to know when a result differs between CI and
your machine: GitHub runners have `jq`, so `test_ollama_model_switch.sh`
runs there and skips in the cicd_runner worker — confirmed on the first
hosted run, where 13 suites executed and none skipped; and `client` is
not run in CI, since `containers-mock` covers the same probe with a
server it starts itself.

Action versions are pinned to majors that declare the Node 24 runtime
(`checkout@v6`, `setup-python@v6`, `setup-node@v6`). Node 20 is removed
from GitHub runners in September 2026, and an action declaring it emits
a deprecation warning until then.

## Scope

An LLM eval harness. Changes should serve the eval and scoring workflow
— Terraform and Compose deployment, the CVV scoring layer, model
discovery and the picker UI. Not a general-purpose Ollama or opencode
wrapper.

## Maintenance

Maintained by one person, with AI-assisted development visible in the
commit history. Response times reflect that.

## Private data

This repo is public. Do not commit hostnames, local usernames, absolute
host paths or credentials, including in log excerpts attached to issues.
`make verify` fails on host paths, usernames and the development host
name; it does not catch everything.
