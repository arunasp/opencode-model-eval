# Contributing

This repo's day-to-day workflow (`docs/BRANCHING.md`) is a fast, direct
solo path — the maintainer commits straight to `main` for their own
changes, no PR review required. If you're contributing from outside,
you go through a normal pull request instead. Both are the expected
shape of this project; neither is a workaround for the other.

## Before you start

For anything more than a trivial fix, open an issue first (there are
bug report and feature request templates) — it's a much shorter
conversation before code gets written than after.

## Making a change

1. Fork the repo, branch off a freshly-fetched `main`.
2. Name the branch `feat/`, `fix/`, `docs/`, or `chore/` followed by a
   short description — see `docs/BRANCHING.md` for what each prefix
   means.
3. Make the change. Keep it focused — a PR that does one thing is much
   faster to review than one that does three.
4. Verify it for real, matching what you touched:
   - Bash: `shellcheck` on anything you changed
   - Terraform: `terraform validate` (or the HCL2 parser if you don't
     have the terraform binary handy)
   - Docker: an actual `docker compose build` / `docker build`
   - Anything behavior-visible (harness-control.sh, the pickers, the
     eval flow): describe what you ran and what you saw — a syntax
     check isn't a substitute for actually trying the thing
5. Open the PR against `main`. The template will prompt for the above.

## Scope of this repo

This is an LLM eval harness, not a general-purpose Ollama/opencode
wrapper — changes should serve the eval/scoring workflow (Terraform +
Docker Compose deployment, the CVV scoring layer, the model discovery
and picker UI) rather than add unrelated functionality.

## A note on scale

This project is maintained by one person (plus AI-assisted
development — you'll see that in the commit history, and that's
intentional, not something to work around). Response times on issues
and PRs will reflect that; it's not a signal about how welcome a
contribution is.

## Private data

This repo is public. Never commit hostnames, local usernames, absolute
host filesystem paths, or credentials — scrub them from anything
before it goes in a commit, including log excerpts attached to issues.
