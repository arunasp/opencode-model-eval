# BRANCHING.md

`main` plus short-lived topic branches. Deliberately not GitFlow, not a
PR-review-gated model — this is sized for one contributor plus one
assistant, not a team, same rationale as `opencode-plugin-ctx-squid`'s
`BRANCHING.md`.

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
