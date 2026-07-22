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

_host_arrow_menu_draw() {
  local -n _ham_header="$1"
  local -n _ham_opts="$2"
  local _ham_idx="$3"
  local i
  echo "$_ham_header" >&2
  for i in "${!_ham_opts[@]}"; do
    if [ "$i" -eq "$_ham_idx" ]; then
      printf '  \033[7m> %s\033[0m\n' "${_ham_opts[$i]}" >&2
    else
      printf '    %s\n' "${_ham_opts[$i]}" >&2
    fi
  done
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

  _host_arrow_menu_draw header options "$idx"
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
    printf '\033[%dA' "$((count + 1))" >&2
    _host_arrow_menu_draw header options "$idx"
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

  local -a full_ids=()
  while IFS= read -r line; do
    full_ids+=("$line")
  done < <(printf '%s' "$candidates_json" | jq -r '.[].full_id')

  host_arrow_menu \
    "Discovered ${count} free candidate(s) -- Up/Down (or j/k) to move, Enter to select, q to cancel:" \
    "${full_ids[@]}"
}
