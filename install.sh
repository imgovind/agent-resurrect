#!/usr/bin/env bash
# agent-resurrect installer for macOS. https://github.com/imgovind/agent-resurrect
#
#   curl -fsSL https://github.com/imgovind/agent-resurrect/releases/latest/download/install.sh | bash
#   ./install.sh                (from a clone or the Claude Code plugin: installs those files, no download)
#   ./install.sh --dry-run      (shows what "agent-resurrect install" would change; writes nothing in HOME)
#
# It copies agent-resurrect to ~/.local/share/agent-resurrect, writes the launcher ~/.local/bin/agent-resurrect
# and the "resurrect" alias, then runs "agent-resurrect install". That applies each setting and records its
# original value first, so "agent-resurrect uninstall" restores it. Finally it runs "agent-resurrect doctor".
# It never uses sudo and only writes inside your home directory and your user defaults.
#
# Everything runs inside functions called from the last line, so a partly downloaded script does nothing.
# Environment: AGENT_RESURRECT_REF picks a branch, tag or commit to download instead of this release;
# XDG_DATA_HOME is honoured.
set -euo pipefail

DEFAULT_REF="v0.3.0"
# The install.sh attached to each GitHub release has this set, by .github/workflows/release.yml, to the full
# commit SHA that DEFAULT_REF named when it was released. A tag can be moved and a commit cannot, so that
# installer downloads exactly the code that was released. It is set here only, never from the environment.
AGENT_RESURRECT_COMMIT=""
REPO="imgovind/agent-resurrect"
# What gets installed: the code, and the Claude Code plugin (whose root is the install directory).
ITEMS="src libexec bin shell hooks skills .claude-plugin install.sh README.md LICENSE"
DRY_RUN=0
PYTHON=""
SRC_DIR=""
TMP_DIR=""
DATA=""
BIN=""

say() { printf '%s\n' "$*"; }
warn() { printf 'agent-resurrect: warning: %s\n' "$*" >&2; }
die() {
  printf 'agent-resurrect: %s\n' "$*" >&2
  exit 1
}

usage() {
  say "usage: install.sh [--dry-run]"
  say "  --dry-run  show what the installer would change, and change nothing"
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        usage >&2
        exit 2
        ;;
    esac
    shift
  done
}

cleanup() {
  if [ -n "$TMP_DIR" ]; then rm -rf "$TMP_DIR"; fi
  if [ -n "$DATA" ] && [ -d "$DATA.new" ]; then rm -rf "$DATA.new"; fi
}

preflight() {
  if [ "$(uname -s)" != "Darwin" ]; then
    die "agent-resurrect supports macOS only."
  fi
  if [ "$(id -u)" = "0" ]; then
    die "Do not run the installer as root or with sudo; run it as yourself."
  fi
  if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
    die "HOME is not set to a directory."
  fi
  if ! command -v claude >/dev/null 2>&1; then
    warn "Claude Code (claude) is not on PATH, so the plugin step is skipped. Install Claude Code, then run: agent-resurrect install"
  fi
  if [ ! -d "${AGENT_RESURRECT_ITERM_APP:-/Applications/iTerm.app}" ]; then
    warn "iTerm2 is not installed, so the iTerm2 steps are skipped. Install it from https://iterm2.com, then run: agent-resurrect install"
  fi
}

python_ok() {
  # -B: Apple's Python otherwise caches bytecode in ~/Library/Caches/com.apple.python, which --dry-run must not.
  "$1" -B -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

find_python() {
  local candidate=""
  # /usr/bin/python3 is a stub that opens the Command Line Tools install dialog when they are missing,
  # so it is used only when xcode-select -p says they are installed.
  if xcode-select -p >/dev/null 2>&1 && [ -x /usr/bin/python3 ] && python_ok /usr/bin/python3; then
    PYTHON="/usr/bin/python3"
    return 0
  fi
  candidate="$(command -v python3 2>/dev/null || true)"
  if [ -n "$candidate" ] && [ "$candidate" != "/usr/bin/python3" ] && python_ok "$candidate"; then
    case "$candidate" in
      /*) ;;
      *) candidate="$(cd "$(dirname "$candidate")" && pwd -P)/$(basename "$candidate")" ;;
    esac
    case "$candidate" in
      # pyenv and asdf shims choose a Python per directory; pin the interpreter they chose now.
      */shims/*) candidate="$("$candidate" -B -c 'import sys; print(sys.executable)')" ;;
    esac
    PYTHON="$candidate"
    return 0
  fi
  die "Python 3.9+ is required. Install Apple's command line tools with: xcode-select --install"
}

script_dir() {
  local source="${BASH_SOURCE[0]:-}"
  if [ -n "$source" ] && [ -f "$source" ]; then
    (cd "$(dirname "$source")" && pwd -P)
  fi
}

make_tmp() {
  if [ -z "$TMP_DIR" ]; then
    TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agent-resurrect.XXXXXX")"
  fi
}

download() {
  local ref="${AGENT_RESURRECT_REF:-}" label url
  if [ -n "$ref" ]; then
    label="$ref"
  elif [ -n "$AGENT_RESURRECT_COMMIT" ]; then
    case "$AGENT_RESURRECT_COMMIT" in
      *[!0-9a-f]*) die "AGENT_RESURRECT_COMMIT in this script is not a full commit SHA: $AGENT_RESURRECT_COMMIT" ;;
    esac
    if [ "${#AGENT_RESURRECT_COMMIT}" -ne 40 ]; then
      die "AGENT_RESURRECT_COMMIT in this script is not a full commit SHA: $AGENT_RESURRECT_COMMIT"
    fi
    ref="$AGENT_RESURRECT_COMMIT"
    label="$DEFAULT_REF (commit $AGENT_RESURRECT_COMMIT)"
  else
    ref="$DEFAULT_REF"
    label="$ref"
  fi
  url="https://codeload.github.com/$REPO/tar.gz/$ref"
  make_tmp
  say "Downloading agent-resurrect $label"
  if ! curl -fsSL "$url" | tar -xzf - -C "$TMP_DIR"; then
    die "could not download $url"
  fi
  SRC_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [ -z "$SRC_DIR" ] || [ ! -f "$SRC_DIR/src/agent_resurrect/__init__.py" ]; then
    die "the download from $url does not contain agent-resurrect"
  fi
}

locate_source() {
  local here
  here="$(script_dir)"
  if [ -n "$here" ] && [ -f "$here/src/agent_resurrect/__init__.py" ]; then
    SRC_DIR="$here"
    if [ "$DRY_RUN" -eq 1 ]; then say "Checking the files in $SRC_DIR"; else say "Installing from $SRC_DIR"; fi
  else
    download
  fi
}

copy_tree() {
  # Copy the installed items from SRC_DIR into a new directory $1, without bytecode.
  local dest="$1" item
  rm -rf "$dest"
  mkdir -p "$dest"
  for item in $ITEMS; do
    if [ -e "$SRC_DIR/$item" ]; then
      cp -R "$SRC_DIR/$item" "$dest/"
    fi
  done
  find "$dest" -name __pycache__ -type d -prune -exec rm -rf {} +
}

fetch() {
  locate_source
  DATA="${XDG_DATA_HOME:-$HOME/.local/share}/agent-resurrect"
  copy_tree "$DATA.new"
  rm -rf "$DATA"
  mv "$DATA.new" "$DATA"
}

sh_quote() {
  # Single-quote a word for sh: a'b becomes 'a'\''b'.
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

write_launchers() {
  local launcher tmp
  BIN="$HOME/.local/bin"
  mkdir -p "$BIN"
  launcher="$BIN/agent-resurrect"
  tmp="$launcher.tmp.$$"
  {
    printf '#!/bin/sh\n'
    # A dry run (install --dry-run, uninstall --dry-run) writes nothing under HOME, not even bytecode.
    # shellcheck disable=SC2016  # "$@" and $arg are meant literally: they belong to the launcher
    printf 'for arg in "$@"; do [ "$arg" = --dry-run ] && exec %s -I -B %s "$@"; done\n' \
      "$(sh_quote "$PYTHON")" "$(sh_quote "$DATA/libexec/agent-resurrect.py")"
    # pycache_prefix: the system Python's standard library has no bytecode and is read-only, so without a
    # writable cache every run (each Claude Code hook) would recompile it: about 155 ms instead of 45 ms.
    # shellcheck disable=SC2016  # "$@" is meant literally: it belongs to the launcher, not to this script
    printf 'exec %s -I -X %s %s "$@"\n' "$(sh_quote "$PYTHON")" \
      "$(sh_quote "pycache_prefix=$HOME/.cache/agent-resurrect/pycache")" \
      "$(sh_quote "$DATA/libexec/agent-resurrect.py")"
  } >"$tmp"
  chmod 755 "$tmp"
  mv -f "$tmp" "$launcher"
  if [ -L "$BIN/resurrect" ] && [ "$(readlink "$BIN/resurrect")" != "agent-resurrect" ]; then
    say "Note: $BIN/resurrect already points somewhere else, so the resurrect alias was not created."
  elif [ -e "$BIN/resurrect" ] && [ ! -L "$BIN/resurrect" ]; then
    say "Note: $BIN/resurrect already exists, so the resurrect alias was not created."
  else
    ln -sfn agent-resurrect "$BIN/resurrect"
  fi
}

run_install() {
  local status=0
  "$BIN/agent-resurrect" install --yes </dev/null || status=$?
  "$BIN/agent-resurrect" doctor </dev/null || true
  if [ "$status" -ne 0 ]; then
    die "some install steps failed (see above). Fix them, then run: agent-resurrect install"
  fi
  say ""
  say "Installed. Open a new terminal tab to activate. Uninstall any time with: agent-resurrect uninstall"
}

dry_run() {
  # Stage exactly what an install would copy in a temporary directory and ask it what it would change.
  # -B: no bytecode is written anywhere, so nothing lands in HOME.
  local status=0
  locate_source
  make_tmp
  copy_tree "$TMP_DIR/stage"
  say "Dry run: nothing is written to your home directory or your settings."
  "$PYTHON" -I -B "$TMP_DIR/stage/libexec/agent-resurrect.py" install --dry-run </dev/null || status=$?
  say ""
  if [ "$status" -ne 0 ]; then
    die "the dry run failed (see above)."
  fi
  say "Dry run finished. Run the installer without --dry-run to make these changes."
}

main() {
  trap cleanup EXIT
  parse_args "$@"
  preflight
  find_python
  if [ "$DRY_RUN" -eq 1 ]; then
    dry_run
    return
  fi
  fetch
  write_launchers
  run_install
}

main "$@"
