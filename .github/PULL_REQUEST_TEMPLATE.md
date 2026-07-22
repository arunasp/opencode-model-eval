<!--
Thanks for the PR! A few things that make review faster:
- Branch name follows feat/ fix/ docs/ chore/ (see docs/BRANCHING.md)
- Keep unrelated changes out of this PR — smaller, focused diffs review faster
-->

## What this changes and why

<!-- What's the problem, and what does this PR do about it? -->

## How it was verified

<!--
Match the verification to what you touched, per docs/BRANCHING.md's
own workflow step 3 — not just a syntax check standing in for a real
test:
  - Bash: shellcheck output
  - Terraform: HCL2 parse or `terraform validate`/`plan`
  - Docker: a real `docker compose build` / `docker build`
  - Behavior changes to harness-control.sh or the pickers: describe
    what you actually ran and what you saw (screenshots/terminal
    output welcome)
-->

## Checklist

- [ ] Branch name follows `feat/` `fix/` `docs/` `chore/`
- [ ] Verification above matches what was actually changed (not just "it compiles")
- [ ] No hostnames, local usernames, or absolute host paths introduced (this repo is public)
- [ ] Docs updated if this changes setup, usage, or dependencies
