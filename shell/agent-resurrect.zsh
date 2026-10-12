# agent-resurrect: on the first prompt of an interactive shell in a restored terminal pane,
# resume the agent session that was running there. Spawns nothing when the pane has no record.
[[ -o interactive ]] || return 0
(( ${_agent_resurrect_done:-0} )) && return 0  # sourcing the rc file again must not re-arm it
typeset -g _agent_resurrect_done=0
_agent_resurrect_first_prompt() {
  (( _agent_resurrect_done )) && return 0
  _agent_resurrect_done=1
  add-zsh-hook -d precmd _agent_resurrect_first_prompt
  # Only a shell iTerm2 started in the pane owns it: inside tmux, screen, Zellij or an editor's terminal,
  # ITERM_SESSION_ID is inherited, and every shell there would resume the same session (as terminals/iterm.py).
  [[ "${TERM_PROGRAM:-}" == iTerm.app && -z "${TMUX:-}${STY:-}${ZELLIJ:-}" ]] || return 0
  local key=""
  [[ -n "${ITERM_SESSION_ID:-}" ]] && key="iterm-${ITERM_SESSION_ID#*:}"
  [[ -n "$key" && -f "${XDG_STATE_HOME:-$HOME/.local/state}/agent-resurrect/panes/$key.json" ]] || return 0
  "${AGENT_RESURRECT_BIN:-$HOME/.local/bin/agent-resurrect}" auto
}
autoload -Uz add-zsh-hook
add-zsh-hook precmd _agent_resurrect_first_prompt
