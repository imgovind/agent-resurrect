#!/usr/bin/env bash
# agent-resurrect installer for macOS. https://github.com/imgovind/agent-resurrect
#
#   curl -fsSL https://raw.githubusercontent.com/imgovind/agent-resurrect/main/install.sh | bash
#   ./install.sh        (from a clone: installs the working tree instead of downloading)
#
# It copies agent-resurrect to ~/.local/share/agent-resurrect, writes the launcher ~/.local/bin/agent-resurrect
# and the "resurrect" alias, then runs "agent-resurrect install". That applies each setting and records its
# original value first, so "agent-resurrect uninstall" restores it. Finally it runs "agent-resurrect doctor".
# It never uses sudo and only writes inside your home directory and your user defaults.
#
# Everything runs inside functions called from the last line, so a partly downloaded script does nothing.
# Environment: AGENT_RESURRECT_REF picks the release to download (default below); XDG_DATA_HOME is honoured.
set -euo pipefail

DEFAULT_REF="v0.1.0"
REPO="imgovind/agent-resurrect"
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
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
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
      */shims/*) candidate="$("$candidate" -c 'import sys; print(sys.executable)')" ;;
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

download() {
  local ref="${AGENT_RESURRECT_REF:-$DEFAULT_REF}"
  local url="https://codeload.github.com/$REPO/tar.gz/$ref"
  TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agent-resurrect.XXXXXX")"
  say "Downloading agent-resurrect $ref"
  if ! curl -fsSL "$url" | tar -xzf - -C "$TMP_DIR"; then
    die "could not download $url"
  fi
  SRC_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [ -z "$SRC_DIR" ] || [ ! -f "$SRC_DIR/src/agent_resurrect/__init__.py" ]; then
    die "the download from $url does not contain agent-resurrect"
  fi
}

fetch() {
  local here item
  here="$(script_dir)"
  if [ -n "$here" ] && [ -f "$here/src/agent_resurrect/__init__.py" ]; then
    SRC_DIR="$here"
    say "Installing from $SRC_DIR"
  else
    download
  fi
  DATA="${XDG_DATA_HOME:-$HOME/.local/share}/agent-resurrect"
  rm -rf "$DATA.new"
  mkdir -p "$DATA.new"
  for item in src bin shell plugin .claude-plugin install.sh README.md LICENSE; do
    if [ -e "$SRC_DIR/$item" ]; then
      cp -R "$SRC_DIR/$item" "$DATA.new/"
    fi
  done
  find "$DATA.new" -name __pycache__ -type d -prune -exec rm -rf {} +
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
    # shellcheck disable=SC2016  # "$@" is meant literally: it belongs to the launcher, not to this script
    printf 'exec %s -I %s "$@"\n' "$(sh_quote "$PYTHON")" "$(sh_quote "$DATA/bin/agent-resurrect.py")"
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

main() {
  trap cleanup EXIT
  preflight
  find_python
  fetch
  write_launchers
  run_install
}

main "$@"
