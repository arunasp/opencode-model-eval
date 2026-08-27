#!/usr/bin/env python3
"""Flag filler words in prose. No dependency, no network.

WHY THIS IS A CHECK AND NOT A CONVENTION. Style rules held by intention
decay -- this repo has the evidence twice over: the development host
name sat in tracked files for weeks under a check that only counted it,
and the same intensifiers reappeared in text rewritten minutes earlier
specifically to remove them. A rule that fires on the write path holds;
one that lives in a contributing guide does not.

SOURCES. The word list is not invented here. It follows the Google
developer documentation style guide (developers.google.com/style), which
Kubernetes and Dart among others use, plus CircleCI's docs style guide
and the OpenStack writing guidelines:

  - timeless documentation: no now / new / soon / currently / latest,
    which date the text and go stale silently
  - no simply / easily / easy: they assert the reader's experience
  - no please: needless politeness in reference material
  - no adverbs that weaken meaning: really, very, extremely
  - Google keeps `just` only where it contrasts two approaches, so it is
    listed as a warning rather than an error

PROJECT ADDITIONS. `real`, `actual`, `genuine` and their adverbs are
emphasis rather than information in almost every use here. They have one
legitimate job -- distinguishing a live dependency from a mock, as in
"against a real Ollama" versus the mock backend -- so a line may opt out
with a trailing `# noqa: prose` (or `<!-- noqa: prose -->` in Markdown)
rather than being silently exempt.

Usage:
    python3 scripts/tools/prose_check.py <path>...    report and exit 1
    python3 scripts/tools/prose_check.py --count <path>...   count only
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# (pattern, why). Kept as whole words, case-insensitive.
ERRORS = [
    (r"simply", "asserts the reader's experience; delete or rewrite"),
    (r"easil?y?", "asserts the reader's experience; state the steps instead"),
    (r"please", "needless politeness in reference material"),
    (r"real(?:ly)?", "emphasis, not information; delete, or `# noqa: prose` "
                     "if it distinguishes a live dependency from a mock"),
    (r"actual(?:ly)?", "emphasis, not information; delete"),
    (r"genuine(?:ly)?", "emphasis, not information; delete"),
    (r"very|extremely", "adverb that weakens rather than sharpens"),
    (r"obviously|clearly|of course", "asserts what the reader already knows"),
    (r"currently|for now|at present", "dates the text; state the version or condition"),
    (r"note that", "delete; the sentence stands without it"),
]

WARNINGS = [
    (r"just", "keep only where it contrasts two approaches (Google word list)"),
    (r"deliberately|on purpose", "keep only where it marks a decision "
                                 "a reader might otherwise revert"),
    (r"exactly", "usually deletable"),
]

# STRUCTURAL TELLS, ported from two Vale style packages that target
# machine-written prose: tbhb/vale-ai-tells and JMill/deslop. A word list
# catches vocabulary; these catch shapes, which is where the tell
# actually lives. Regexes rather than Vale itself, to avoid adding a Go
# binary to the worker for six rules.
#
# Not ported: intensifier density and paragraph-rhythm metrics, which
# need a real parser rather than a line-at-a-time regex. Vale's own docs
# make the point -- a rule that cannot tell a heading from a code block
# is a rule you end up switching off.
STRUCTURAL = [
    (r"\b(?:it'?s |that'?s )?not (?:just|only|merely|simply)\b",
     "'not just X' -- state what it is, drop the contrast scaffold"),
    (r"\bit'?s not [^,.]{1,50}[,.] it'?s\b",
     "'It's not X, it's Y' -- keep Y, delete X"),
    (r"^\s*(?:-\s*)?(?:Two|Three|Four|Five|Six|Seven)\s+\w+\s+"
     r"(?:things|reasons|ways|pillars|options|steps|points|items)\b",
     "numbered lead-in -- let the list do the counting"),
    (r"\b(?:delve|tapestry|plethora|seamless(?:ly)?|game.?changer|supercharge)\b",
     "stock AI vocabulary with no one-word swap"),
    (r"\b(?:a testament to|harness the power|in today'?s|rapidly evolving)\b",
     "stock AI phrase"),
    (r"\bwhich is (?:the|exactly) (?:point|whole point)\b|\bthat is the (?:whole )?point\b",
     "rhetorical close; state the point instead"),
]

# Markers are anchored to END OF LINE. Without the anchor, prose that
# DOCUMENTS the syntax suppresses itself -- docs/CODEGEN.md's line
# describing `# noqa: prose` was silently exempt, so the file that
# explains the escape hatch was the one file allowed to ignore it.
NOQA = re.compile(r"(?:#|<!--)\s*noqa:\s*prose\s*(?:-->)?\s*$", re.I)
CODE_FENCE = re.compile(r"^\s*```")

# SUPPRESSION, modelled on the GitHub Docs content linter, which allows
# disabling a rule for a whole file, a section, a specific line, or the
# next line. A trailing comment alone forces you to touch a line you may
# not want to reflow, and pushes people to delete the check instead.
#
#   <!-- prose-disable-file -->        rest of the file exempt
#   <!-- prose-disable -->             start of an exempt section
#   <!-- prose-enable -->              end of it
#   <!-- prose-disable-next-line -->   the following line only
#   ... text  # noqa: prose           that line only
#
# `#` works in place of `<!--` for shell and Python files.
# DISABLE_FILE is matched against the WHOLE file text, not line by line,
# so it needs re.M for `$` to mean end-of-line rather than end-of-file.
# Without it the marker only worked on the last line of a file -- caught
# by a fixture, not by reading.
DISABLE_FILE = re.compile(r"(?:#|<!--)\s*prose-disable-file\s*(?:-->)?\s*$", re.I | re.M)
DISABLE_NEXT = re.compile(r"(?:#|<!--)\s*prose-disable-next-line\s*(?:-->)?\s*$", re.I)
DISABLE_START = re.compile(r"(?:#|<!--)\s*prose-disable\s*(?:-->)?\s*$", re.I)
DISABLE_END = re.compile(r"(?:#|<!--)\s*prose-enable\s*(?:-->)?\s*$", re.I)

# RATCHET, not a snapshot. A file listed here carries hits that predate
# the check; it must only ever go DOWN, and the entry is deleted at
# zero. A file absent from the table must be clean.
#
# The table is EMPTY as of 2026-08-27: every tracked doc passes. It
# stays because the mechanism is the point -- the next batch of
# pre-existing hits (a new doc, an imported file, a widened rule set)
# gets a temporary entry rather than either a permanently red stage or a
# check that reports and forgives.
#
# Reporting a count and failing on nothing is what the development-host
# -name check did for weeks, and it removed nothing in that time.
# Failing on the whole backlog at once leaves the stage permanently red,
# which is the same outcome by a different route.
#
# The same shape is the GitHub Docs content linter's stated policy:
# errors block a merge, warnings do not, and "most rules will eventually
# be promoted to errors, once the content no longer has warning
# violations".
#
# RE-SEEDING. Adding a rule raises counts without anyone writing worse
# prose, so the numbers are re-seeded when the rule set changes, and the
# change is recorded here rather than absorbed. Between re-seeds, DOWN
# is the only permitted direction.
#
#   2026-08-27  seeded at 97 from the word list
#   2026-08-27  re-seeded to 102 after porting six structural rules from
#               tbhb/vale-ai-tells and JMill/deslop; the five new hits
#               were all `not just X`
#   2026-08-27  docs/BRANCHING.md and docs/CODEGEN.md rewritten; 102 -> 77
#   2026-08-27  VERSIONING, REQUIREMENTS, README, INSTALL cleaned; 77 -> 27
#   2026-08-27  CHANGELOG cleaned, including its released sections;
#               27 -> 0, table empty
BASELINE: dict[str, int] = {}


def scan(path: Path, honour_suppression: bool = True):
    """Yield (lineno, level, word, why, line) for each hit.

    Fenced code blocks and indented code are skipped: a word list has no
    business editing a command someone has to type.

    With honour_suppression=False the markers are ignored, which is what
    dead_suppressions() uses to find exemptions nothing needs.
    """
    hits = []
    in_fence = False
    in_disabled_section = False
    skip_next = False
    text = path.read_text(errors="replace")
    if honour_suppression and DISABLE_FILE.search(text):
        return hits
    for n, line in enumerate(text.splitlines(), 1):
        if CODE_FENCE.match(line):
            in_fence = not in_fence
            continue
        if DISABLE_END.search(line):
            in_disabled_section = False
            continue
        if DISABLE_START.search(line):
            in_disabled_section = True
            continue
        suppressed_here = skip_next
        skip_next = DISABLE_NEXT.search(line) is not None
        if skip_next:
            continue
        if in_fence or line.startswith("    "):
            continue
        if honour_suppression and (in_disabled_section or suppressed_here
                                   or NOQA.search(line)):
            continue
        for level, table in (("error", ERRORS), ("warning", WARNINGS)):
            for pattern, why in table:
                for m in re.finditer(rf"\b(?:{pattern})\b", line, re.I):
                    hits.append((n, level, m.group(0), why, line.strip()))
        # Structural patterns carry their own anchoring, so they are not
        # wrapped in \b like the word list above.
        for pattern, why in STRUCTURAL:
            for m in re.finditer(pattern, line, re.I):
                hits.append((n, "error", m.group(0)[:40], why, line.strip()))
    return hits


def dead_suppressions(path: Path):
    """Suppressions covering lines that would pass without them.

    A suppression that is not suppressing anything is worse than no
    suppression: it silently exempts whatever is written on that line
    NEXT, and nothing reports it. This is the same class as a stale
    BASELINE number, except the ratchet prints a nudge when a count
    drops and there is no equivalent signal for an exemption that has
    outlived its cause -- so it becomes one here.

    Found by scanning twice: once honouring the markers, once ignoring
    them. Any line carrying a marker that produces no hit in the second
    pass is dead.
    """
    text = path.read_text(errors="replace")
    unsuppressed = {n for n, *_ in scan(path, honour_suppression=False)}
    dead = []
    lines = text.splitlines()

    if DISABLE_FILE.search(text) and not unsuppressed:
        dead.append((0, "prose-disable-file", "the file has no hits without it"))

    in_section = False
    section_start = 0
    section_hits = False
    for n, line in enumerate(lines, 1):
        if DISABLE_END.search(line):
            if in_section and not section_hits:
                dead.append((section_start, "prose-disable section",
                             "no hits between it and prose-enable"))
            in_section = False
            continue
        if DISABLE_START.search(line):
            in_section, section_start, section_hits = True, n, False
            continue
        if in_section and n in unsuppressed:
            section_hits = True
        if DISABLE_NEXT.search(line) and (n + 1) not in unsuppressed:
            dead.append((n, "prose-disable-next-line",
                         "the following line has no hits without it"))
        if NOQA.search(line) and n not in unsuppressed:
            dead.append((n, "noqa: prose", "the line has no hits without it"))
    if in_section and not section_hits:
        dead.append((section_start, "prose-disable section",
                     "unterminated, and no hits after it"))
    return dead


def check_baseline(paths):
    """Enforce the ratchet. Returns an exit code.

    Three outcomes, each reported by name rather than as a total:
    a file over its baseline fails; a file under it prints the new
    number to copy into BASELINE; a file with no entry must be clean.
    """
    rc = 0
    improved = []
    for path in paths:
        if not path.is_file():
            continue
        key = str(path)
        # A dead suppression fails regardless of the baseline. The
        # baseline forgives hits that predate the check; it does not
        # forgive an exemption that was never needed.
        for lineno, marker, why in dead_suppressions(path):
            where = f"{key}:{lineno}" if lineno else key
            print(f"FAIL: {where}: dead suppression ({marker}) -- {why}")
            rc = 1
        errors = sum(1 for _, level, *_ in scan(path) if level == "error")
        allowed = BASELINE.get(key)
        if allowed is None:
            if errors:
                print(f"FAIL: {key}: {errors} error(s), and no baseline entry")
                for n, level, word, why, line in scan(path):
                    if level == "error":
                        print(f"  {key}:{n}: {word} -- {why}")
                rc = 1
            continue
        if errors > allowed:
            print(f"FAIL: {key}: {errors} error(s), baseline allows {allowed}")
            rc = 1
        elif errors < allowed:
            improved.append((key, allowed, errors))
        else:
            print(f"  {key}: {errors} (at baseline)")
    for key, was, now in improved:
        print(f"  {key}: {was} -> {now} -- lower BASELINE in "
              f"scripts/tools/prose_check.py, or delete the entry at 0")
    remaining = sum(BASELINE.values())
    print(f"prose baseline: {remaining} known hit(s) across "
          f"{len(BASELINE)} file(s) still to clean")
    return rc


def main(argv):
    if "--baseline" in argv:
        return check_baseline([Path(a) for a in argv if not a.startswith("--")])
    count_only = "--count" in argv
    paths = [Path(a) for a in argv if not a.startswith("--")]
    errors = warnings = 0
    for path in paths:
        if not path.is_file():
            continue
        for n, level, word, why, line in scan(path):
            if level == "error":
                errors += 1
            else:
                warnings += 1
            if not count_only:
                print(f"{path}:{n}: {level}: {word} -- {why}")
                print(f"    {line[:100]}")
    print(f"prose: {errors} error(s), {warnings} warning(s) "
          f"across {len(paths)} file(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
