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

`make ci` runs lint, test, verify, e2e and client. Stages skip rather
than fail when a dependency is absent, and report the reason.

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
