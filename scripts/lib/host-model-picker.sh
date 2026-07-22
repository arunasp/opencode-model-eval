# scripts/lib/host-model-picker.sh -- arrow-key (or j/k) menu run
# entirely on the host, never inside Docker. Sourced by
# select-and-run-eval.sh and tf-select-and-run-eval.sh; not meant to
# be run standalone (no shebang/executable bit on purpose).
#
# Why host-side, not in-container curses: the harness Docker image's
# base only apt-installs bare python3 (no confirmed ncurses/TUI
# terminal setup), and `docker run -it` adds real complexity for
# something that doesn't need to touch the container at all -- picking
# a model from a list the container already handed back as JSON is a
# pure host-terminal concern. An earlier attempt put the arrow-key menu
# inside the container via Python's curses module (git history: "feat:
# ncurses arrow-key menu for the discovery picker") -- never merged,
# superseded by this file.
#
# Pure bash + raw stty mode + ANSI cursor control, no external TUI
# library: Up/Down or j/k to move, Enter to select, q or a lone Esc to
# cancel. Lone-Esc is disambiguated from the leading byte of an
# arrow-key escape sequence with a short (`read -t`) timeout waiting
# for the rest of the sequence -- the standard, correct way to do this
# (the curses attempt this replaces got bitten by exactly this
# ambiguity, confirmed via a real pty test before it was ever shipped).
#
# Usage (from a sourcing script):
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/host-model-picker.sh"
#   selected="$(host_model_picker "$candidates_json")" || exit 1
#   # $selected is "provider/id" on success; non-zero exit + no
#   # stdout output means cancelled or nothing to choose from.
#
# $candidates_json must be a JSON array of {"provider":...,"model":...,
# "full_id":...} objects -- exactly what
# `discover_and_select_model.py --list-json` prints. Requires jq and a
# real controlling terminal (/dev/tty); callers should check
# [ -t 0 ] themselves before calling this and fall back to a plain
# prompt otherwise -- this function does not do that check itself.

# _host_arrow_menu_draw <header-varname> <options-array-varname> <idx> <rows-out-varname> <window_start> <window_size>
# Prints the header + a WINDOW of the option list (not necessarily all
# of it -- see windowing note below), and writes the ACTUAL number of
# physical terminal rows consumed (via the rows-out nameref) so the
# caller's cursor-up redraw can move back exactly that far.
#
# Bug fixed here (pinned, screenshot evidence from a real 125-
# candidate/9-provider run): the caller used to assume 1 printed line
# == 1 physical row and moved the cursor up by a fixed count+1. That
# breaks the moment any line is wider than the pane -- the provider
# picker's header ("Discovered 125 free candidate(s) across 9
# provider(s) -- pick a provider:", ~74 chars) wraps to multiple rows
# in a ~35-36-col-wide menu pane, so the fixed-count cursor-up moved up
# too few rows, leaving fragments of the old and new frames overlapping
# on screen. Earlier tests never caught this because their synthetic
# labels/counts were short enough to never wrap in a narrow pane.
#
# Second bug fixed here, found using a real 78-option provider list
# (nvidia) in a real 40-row pane: printing EVERY option regardless of
# terminal height doesn't just look bad, it breaks the redraw entirely
# -- terminal cursor-up (CUU) is clamped at row 1 of the visible
# viewport, so if the initial draw is taller than the terminal, the
# top of it has already scrolled off-screen before any redraw ever
# runs, and "move up N rows" can't reach back to where the content
# logically started. Confirmed live: with 78 options in a 40-row pane,
# every redraw just reprinted the whole 79-line block again below
# itself -- unbounded growth (630+ lines after a handful of key
# presses), not a cosmetic issue. Fix: only ever render a WINDOW of
# options that's guaranteed to fit (window_size, computed by the
# caller from real terminal height), scrolled to keep the current
# selection visible -- so the total printed height is always bounded
# regardless of how many options exist, and cursor-up math stays valid.
_host_arrow_menu_draw() {
  local -n _ham_header="$1"
  local -n _ham_opts="$2"
  local _ham_idx="$3"
  local -n _ham_rows_out="$4"
  local _ham_window_start="$5"
  local _ham_window_size="$6"
  local cols
  cols="$(tput cols 2>/dev/null)"
  if [ -z "${cols}" ] || [ "${cols}" -le 0 ] 2>/dev/null; then
    cols=80
  fi

  local i line_text len rows total=0
  local count="${#_ham_opts[@]}"
  local window_end=$(( _ham_window_start + _ham_window_size - 1 ))
  [ "${window_end}" -ge "${count}" ] && window_end=$((count - 1))

  printf '%s\033[K\n' "${_ham_header}" >&2
  len="${#_ham_header}"
  rows=$(( (len + cols - 1) / cols ))
  [ "${rows}" -lt 1 ] && rows=1
  total=$((total + rows))

  if [ "${_ham_window_start}" -gt 0 ]; then
    printf '  ... %s more above ...\033[K\n' "${_ham_window_start}" >&2
    total=$((total + 1))
  fi

  for ((i = _ham_window_start; i <= window_end; i++)); do
    if [ "${i}" -eq "${_ham_idx}" ]; then
      line_text="  > ${_ham_opts[$i]}"
      printf '  \033[7m> %s\033[0m\033[K\n' "${_ham_opts[$i]}" >&2
    else
      line_text="    ${_ham_opts[$i]}"
      printf '    %s\033[K\n' "${_ham_opts[$i]}" >&2
    fi
    len="${#line_text}"
    rows=$(( (len + cols - 1) / cols ))
    [ "${rows}" -lt 1 ] && rows=1
    total=$((total + rows))
  done

  if [ "${window_end}" -lt $((count - 1)) ]; then
    printf '  ... %s more below ...\033[K\n' "$((count - 1 - window_end))" >&2
    total=$((total + 1))
  fi

  # Persistent shortcuts footer -- shown on every menu everywhere,
  # since this is the shared draw primitive every host_arrow_menu call
  # goes through. Plain ASCII (no arrow glyphs) to avoid any locale-
  # dependent width-counting surprises in the row-span math above,
  # matching the plain-text style already used throughout this file.
  printf '  Up/Down or j/k: move   Enter: select   q: cancel\033[K\n' >&2
  total=$((total + 1))

  # Clear anything below this frame -- fixes a real bug found using
  # 78 real options in a 39-usable-row pane (tmux's own status bar
  # takes 1 row even when the window itself is 40 rows): when a
  # redraw is SHORTER than the previous one (e.g. the "more above"
  # indicator disappearing as the window scrolls back to the top,
  # dropping the total by one row), the leftover row from the taller
  # previous frame was never overwritten -- it just sat there below
  # the new, shorter content. Confirmed live: scrolling back to the
  # top left a stale "43 more below" line visible alongside the
  # freshly-drawn, correct "42 more below" line one row above it.
  printf '\033[J' >&2

  _ham_rows_out="${total}"
}

# host_arrow_menu <header> <option1> [option2 ...]
# Generic arrow-key/j-k highlighted menu -- prints the selected option
# to stdout and returns 0, or prints nothing and returns 1 on cancel
# (q or a lone Esc). The primitive host_model_picker and any other
# host-side picker in this repo should build on.
host_arrow_menu() {
  # shellcheck disable=SC2034  # used via nameref in _host_arrow_menu_draw
  local header="$1"; shift
  local -a options=("$@")
  local count="${#options[@]}"
  if [ "$count" -eq 0 ]; then
    echo "No options to choose from." >&2
    return 1
  fi

  local idx=0 key esc_seq old_stty result_idx=-1
  old_stty="$(stty -g < /dev/tty)"
  # min 1 time 0: read() blocks for exactly one byte, no line
  # buffering, no local echo -- required for single-keypress reads.
  stty -icanon -echo min 1 time 0 < /dev/tty
  # INT is the one case that can genuinely fire asynchronously mid-loop,
  # so it still needs a trap. Deliberately NOT using `trap ... RETURN`
  # here: it is NOT function-scoped without set -T/functrace -- a
  # RETURN trap registered inside a function leaks and fires on every
  # SUBSEQUENT function return for the rest of the process, not just
  # this call. Confirmed live in harness-control.sh: a second, unrelated
  # function's return tried to evaluate this function's already-out-of-
  # scope $old_stty from a stale leaked trap and errored with "unbound
  # variable" under set -u -- only surfaced once this got called more
  # than once in the same process (a standalone smoke test calling it
  # exactly once never would have caught it). Every exit path below
  # restores the terminal explicitly and clears the INT trap before
  # returning, instead.
  trap 'stty "$old_stty" < /dev/tty; echo "Cancelled." >&2; exit 130' INT

  # Clear the pane before drawing -- fixes real friction reported
  # live: this function's OWN internal redraw (during navigation)
  # already stays pinned in place, but every SEPARATE call to
  # host_arrow_menu (backend picker -> provider picker -> model
  # picker -> back to the main menu, etc.) used to just print fresh
  # content below whatever the previous call left on screen, so the
  # pane's visible transcript grew without bound across a session
  # instead of staying fixed -- you had to scroll to find the current
  # menu. Clearing here, once per call, keeps every distinct menu
  # pinned at a fixed position regardless of how many stages came
  # before it in the same pane.
  printf '\033[2J\033[H' >&2

  # Viewport size: how many options can actually fit. Reserve rows for
  # the header (itself could wrap -- use its own row-span, not just 1)
  # plus up to 2 scroll-indicator lines ("more above"/"more below")
  # plus 1 row for the shortcuts footer plus 1 row of margin. Computed
  # from real terminal height (tput lines), same source of truth as
  # the width-wrapping fix above.
  local term_lines header_rows header_len cols_for_window
  term_lines="$(tput lines 2>/dev/null)"
  if [ -z "${term_lines}" ] || [ "${term_lines}" -le 0 ] 2>/dev/null; then
    term_lines=24
  fi
  cols_for_window="$(tput cols 2>/dev/null)"
  if [ -z "${cols_for_window}" ] || [ "${cols_for_window}" -le 0 ] 2>/dev/null; then
    cols_for_window=80
  fi
  header_len="${#header}"
  header_rows=$(( (header_len + cols_for_window - 1) / cols_for_window ))
  [ "${header_rows}" -lt 1 ] && header_rows=1

  local window_size=$(( term_lines - header_rows - 4 ))
  [ "${window_size}" -lt 1 ] && window_size=1
  [ "${window_size}" -gt "${count}" ] && window_size="${count}"
  local window_start=0

  local rows_printed=0
  _host_arrow_menu_draw header options "$idx" rows_printed "${window_start}" "${window_size}"
  while true; do
    IFS= read -rsn1 key < /dev/tty
    case "$key" in
      $'\x1b')
        # Give a brief window for the rest of an arrow-key sequence to
        # arrive; if nothing follows in time, it was a lone Esc.
        if IFS= read -rsn1 -t 0.05 esc_seq < /dev/tty && [ "$esc_seq" = "[" ]; then
          IFS= read -rsn1 -t 0.05 esc_seq < /dev/tty
          case "$esc_seq" in
            A) idx=$(( (idx - 1 + count) % count )) ;;
            B) idx=$(( (idx + 1) % count )) ;;
          esac
        else
          break  # lone Esc -- cancel
        fi
        ;;
      k) idx=$(( (idx - 1 + count) % count )) ;;
      j) idx=$(( (idx + 1) % count )) ;;
      q) break ;;  # cancel
      ""|$'\r'|$'\n')
        result_idx="$idx"
        break
        ;;
      *) : ;;  # unrecognized byte -- ignore, just redraw current state
    esac
    # Keep idx visible within the scroll window -- scroll up or down
    # the minimum amount needed, rather than always re-centering.
    if [ "${idx}" -lt "${window_start}" ]; then
      window_start="${idx}"
    elif [ "${idx}" -gt $((window_start + window_size - 1)) ]; then
      window_start=$((idx - window_size + 1))
    fi
    printf '\033[%dA' "${rows_printed}" >&2
    _host_arrow_menu_draw header options "$idx" rows_printed "${window_start}" "${window_size}"
  done

  stty "$old_stty" < /dev/tty
  trap - INT

  if [ "${result_idx}" -eq -1 ]; then
    echo "Cancelled." >&2
    return 1
  fi
  echo "${options[$result_idx]}"
  return 0
}

host_model_picker() {
  local candidates_json="$1"
  local count
  count="$(printf '%s' "$candidates_json" | jq 'length')"
  if [ "$count" -eq 0 ]; then
    echo "No free candidates to choose from." >&2
    return 1
  fi

  # Stage 1: pick a provider. A flat list of every candidate doesn't
  # scale -- confirmed live, 125 free candidates across dozens of
  # providers in one real discovery run is unusable as a single menu.
  # Labels carry a per-provider count so the list itself gives a sense
  # of scale before committing to one.
  local -a provider_labels=()
  local -a provider_names=()
  while IFS= read -r line; do
    local name="${line%%|*}"
    local n="${line#*|}"
    provider_names+=("$name")
    provider_labels+=("${name} (${n} model$([ "$n" != 1 ] && echo s))")
  done < <(printf '%s' "$candidates_json" | jq -r '
    group_by(.provider) | map({provider: .[0].provider, count: length})
    | sort_by(.provider)[] | "\(.provider)|\(.count)"
  ')

  local picked_provider_label
  picked_provider_label="$(host_arrow_menu \
    "Discovered ${count} free candidate(s) across ${#provider_names[@]} provider(s) -- pick a provider:" \
    "${provider_labels[@]}")" || return 1

  local picked_provider=""
  local i
  for i in "${!provider_labels[@]}"; do
    if [ "${provider_labels[$i]}" = "${picked_provider_label}" ]; then
      picked_provider="${provider_names[$i]}"
      break
    fi
  done

  # Stage 2: pick a model within that provider. Just the model id here
  # (not the full provider/id) since the provider is already fixed --
  # shorter, and avoids repeating it N times down the list.
  local -a model_ids=()
  while IFS= read -r line; do
    model_ids+=("$line")
  done < <(printf '%s' "$candidates_json" | jq -r --arg p "$picked_provider" \
    '[.[] | select(.provider == $p)] | sort_by(.model)[].model')

  local picked_model
  picked_model="$(host_arrow_menu \
    "${picked_provider} -- pick a model:" \
    "${model_ids[@]}")" || return 1

  echo "${picked_provider}/${picked_model}"
}
