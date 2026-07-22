# BRANCHING.md

`main` plus short-lived topic branches. Deliberately not GitFlow, not a
PR-review-gated model for the maintainer's own changes — this workflow
is sized for one contributor plus one assistant, not a team, same
rationale as `opencode-plugin-ctx-squid`'s `BRANCHING.md`.

This describes the maintainer's own fast path. **External
contributions go through a real, reviewed pull request instead** — see
[CONTRIBUTING.md](../CONTRIBUTING.md). The two aren't in tension: one
person moving fast on their own repo and requiring review from
everyone else are the normal, expected shape of an open-source project
with a single maintainer, not a contradiction to resolve.

## Branch naming

`<type>/<short-description>`, where `<type>` is one of:

- `feat/` — new capability (e.g. `feat/why-chain-scoring`)
- `fix/` — bug fix
- `docs/` — documentation only
- `chore/` — tooling, CI, dependency bumps, no behavior change

## Workflow

1. Branch off a freshly-fetched `origin/main` — never a stale local
   `main`, and never a hardcoded commit SHA (a hardcoded base goes stale
   the moment `origin` moves; this has caused a real failure before).
2. Apply changes on the topic branch.
3. Run the actual verification for the change — `shellcheck` for bash,
   the HCL2 parser (or `terraform validate` if you have the binary) for
   Terraform, a real `docker compose build` / `terraform plan` if you
   have Docker available — not just a syntax check standing in for a
   real test.
4. Fast-forward-merge into `main` only if verification passes.
   `--ff-only` is the actual safety check here, not a formality — if it's
   not a fast-forward, `main` moved underneath you and the branch needs
   rebasing first, not a forced merge.
5. Delete the topic branch with `git branch -D`, not `-d` — `-d` refuses
   to delete a branch whose upstream hasn't been pushed yet, which is
   the normal case for a short-lived local topic branch, not an error
   condition to work around.

## Versioning

Semantic versioning (`vMAJOR.MINOR.PATCH`), starting at `v0.1.0` —
pre-1.0 while the CLI/menu surface (harness-control.sh) is still
changing shape often enough that "breaking" isn't yet a meaningful
distinction from "the normal rate of change."

- **PATCH**: bug fixes only, no new capability, no behavior change
  beyond "the thing now works as originally intended."
- **MINOR**: new capability (a new menu entry, a new script flag, a
  new deployment resource) that doesn't remove or change the meaning
  of anything existing.
- **MAJOR**: reserved for `v1.0.0` onward, once the CLI/menu surface
  is stable enough that a genuine breaking change (removing a flag,
  changing a menu's meaning, changing an on-disk format) is a real,
  distinguishable event rather than routine iteration.

Tag **after** merging to `main`, not before — a tag on an unmerged
branch tip is a tag on a commit `main` may never actually reach if
that branch gets rebased or abandoned instead.

```bash
git checkout main && git pull
git tag -a vX.Y.Z -m "<one-line summary of what this checkpoint adds>"
git push origin vX.Y.Z
```

Not every merge needs a tag — routine fixes can ride along until the
next tagged version without their own checkpoint. Tag when a batch of
changes reaches a state worth being able to reference or roll back to
by name, which in practice has been roughly every few merged bundles
so far.

## Delivery convention

For changes after the first commit: `git format-patch` sets, not a full
repo re-dump. Naming: `<project>-patch-x.y.z.tar.gz`, where `x.y.z` is
the version the patch set delivers **to** (the resulting tag), not the
version patched from. Include a standalone `apply-patches.sh` alongside
the `.patch` files — outside the git repo, never committed — that:
aborts any stuck `git am` session, fetches `origin` fresh, branches off
the freshly-fetched `origin/main`, applies patches there, runs
verification on that branch, and only fast-forward-merges into `main` if
it passes.

Full tarballs remain correct for: the repo's first commit (nothing to
diff against yet — this repo's initial state), after a rebase/history
rewrite (old commit hashes the patches were generated against no longer
exist), or when patches fail to apply cleanly and reconciling costs more
than resyncing from a snapshot.
