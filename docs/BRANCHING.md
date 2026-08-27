# Branching

`main` plus short-lived topic branches. Not GitFlow, and no PR review
for the maintainer's own changes: the workflow is sized for one
contributor and one assistant.

External contributions go through a reviewed pull request. See
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Branch naming

`<type>/<short-description>`, where `<type>` is one of:

| Prefix | For |
|---|---|
| `feat/` | New capability, e.g. `feat/why-chain-scoring` |
| `fix/` | Bug fix |
| `docs/` | Documentation only |
| `chore/` | Tooling, CI, dependency bumps; no behaviour change |

## Workflow

1. Branch off a freshly fetched `origin/main`. Never a stale local
   `main`, and never a hardcoded commit SHA — a hardcoded base goes
   stale as soon as `origin` moves, which has broken a patch set here
   before.
2. Apply changes on the topic branch.
3. Verify what you changed: `shellcheck` for bash, the HCL2 parser or
   `terraform validate` for Terraform, `docker compose build` or
   `terraform plan` where a daemon is available. A syntax check does not
   stand in for running the thing.
4. Merge with `--ff-only`. The flag is the check: a merge that is not a
   fast-forward means `main` moved underneath you, and the branch needs
   rebasing rather than a forced merge.
5. Delete the topic branch with `git branch -D`. `-d` refuses to delete
   a branch whose upstream was never pushed, which is the normal state
   of a short-lived local branch.

`make ci` runs lint, prose, test, verify, e2e and client. Stages skip
rather than fail when a dependency is absent, and name the reason.

## Tagging

Version rules live in [VERSIONING.md](VERSIONING.md). Tag **after**
merging to `main`: a tag on an unmerged branch tip names a commit
`main` may never reach if that branch is rebased or abandoned.

```bash
git checkout main && git pull
git tag -a vX.Y.Z -m "<one-line summary of what this checkpoint adds>"
git push origin vX.Y.Z
```

Not every merge needs a tag. Tag when a batch reaches a state worth
referencing or rolling back to by name.

## History rewrites

Squashing or rebasing unpushed commits is fine; anything already on
`origin` is not. Before a rewrite:

1. Tag the current tip — `git tag backup/pre-<reason>-<date>` — so the
   pre-rewrite chain survives a bad rebase.
2. Check for refs pointing into the range being rewritten
   (`git for-each-ref` against `git rev-list origin/main..main`). A
   branch left pointing at a rewritten commit sits on an orphaned chain.
3. Compare `HEAD^{tree}` before and after. A squash must not change
   content; if the trees differ, do not move `main`.

## Delivery convention

Superseded. Changes land as commits on this worktree, verified in the
same place. The `git format-patch` sets and `apply-patches.sh` bundles
described in earlier revisions of this file are retired.

Full tarballs remain correct for a first commit, after a history
rewrite that invalidates the hashes patches were generated against, or
when reconciling a failed patch application costs more than resyncing.
