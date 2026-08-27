# Versioning

`MAJOR.MINOR.BUILD`, applied to the repo as a whole via git tags. No
package is published, so tags are the only version artifact.

**This is not semantic versioning, and saying so matters.** Semver
reserves the third position for PATCH, which carries an implied promise:
same MAJOR.MINOR means compatible, and a higher third digit means fixes
only. Here the third digit is a BUILD COUNTER and carries no such
promise — it says how many merges have landed, not what kind. A reader
who assumes semver will read a build bump as a bugfix release.

| Position | Meaning |
|---|---|
| **MAJOR** | Breaking change to the environment contract: task-suite prompt format, results JSON schema, or required env vars — as of `v1.0.0`, `OPENCODE_MODEL_PROVIDER`, `OPENCODE_MODEL_ID` and the server URL |
| **MINOR** | Additive capability: a new model reachable through the global opencode config, a new results field, a new flag defaulting to prior behaviour |
| **BUILD** | Every merge to `main`, tagged or not. Resets to 0 when MAJOR or MINOR changes |

The Dockerfile's `MODEL_PROVIDER` / `MODEL_ID` build args were once a
MAJOR-level contract. They no longer exist — model selection is a
runtime request parameter as of `v0.2.0`.

## The build digit

It counts merges, not releases, so it advances whether or not anything
is tagged. Two consequences, both correct rather than defects to fix:

- **Tag numbers have gaps.** `v1.1.0` may be followed by `v1.1.4` if
  three untagged merges landed in between. A contiguous run of tag
  numbers would mean the counter was being maintained by hand.
- **Every commit has a version, whether tagged or not.** The build digit
  is derivable from the history rather than remembered:

  ```bash
  # merges on main since the last MAJOR/MINOR bump
  git rev-list --count <last-bump-tag>..HEAD
  ```

Because it resets on a MAJOR or MINOR change, the bump commit itself is
build 0. `v1.1.0` is build 0 of the 1.1 line; the next merge to `main`
is 1.1.1 even if no tag is cut for it.

## Tagging

Tag `main` at the commit reaching a version boundary. Not every build
needs a tag — tag when a state is worth referencing or rolling back to
by name.

## Tags

| Tag | Date | Contents |
|---|---|---|
| `v0.2.0` | 2026-07-25 | Shared harness image; Terraform and Compose deployment paths; scoped auth extraction, automatic under Terraform; the full test ladder; quota and rate-limit handling; per-run server log capture; `git-workspace` and `jupyter` roles; session TTL reaper; static example notebooks; split `README.md` / `INSTALL.md` / `CHANGELOG.md` / `REQUIREMENTS.md` |
| `v1.0.0` | 2026-07-27 | Breaking: `local/ollama` models come from the global opencode config rather than a static project list. Requires `OPENCODE_GLOBAL_CONFIG` |
| `v1.1.0` | 2026-08-27 | Harness defects fixed against measurement (readiness race, unproductive-loop detection, full session capture); provider-request capture proxy; prose and workflow linting; GitHub Actions CI driven entirely by the mock provider; dev dependencies cached in a project-local venv |

`v0.1.0` was planned as a minimal checkpoint and never tagged; the repo
had moved well past that scope by the time a tag was cut. It stands as
the implicit baseline `v0.2.0` is additive on top of.

`v1.0.0`'s CHANGELOG section was never promoted out of `[Unreleased]`
when the tag was cut, so later work accumulated above it and the release
looked unrecorded. Promote the section in the same commit that moves the
tag, or the two disagree silently.

What each release deliberately left undone is **not** in `CHANGELOG.md`.
Keep a Changelog defines six categories — Added, Changed, Deprecated,
Removed, Fixed, Security — and refuses a seventh, so undone work has no
place there: it is not a change. It belongs in README's "Known gaps",
which is where this project already keeps it.

A breaking change is a `Changed` entry marked `**Breaking:**`, not a
heading of its own — same reason.
