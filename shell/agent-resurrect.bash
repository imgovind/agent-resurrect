# agent-resurrect: on the first prompt of an interactive shell in a restored terminal pane,
# resume the agent session that was running there. Spawns nothing when the pane has no record.
case $- in *i*) ;; *) return 0 ;; esac
[ "${_agent_resurrect_done:-0}" = 1 ] && return 0  # sourcing the rc file again must not re-arm it
_agent_resurrect_done=0
_agent_resurrect_first_prompt() {
  [ "$_agent_resurrect_done" = 1 ] && return 0
  _agent_resurrect_done=1
  # Remove this hook wherever it sits (the rc file may have been sourced twice, or another tool may
  # have prepended to PROMPT_COMMAND); a bare leftover becomes the no-op ":" so the rest stays valid.
  PROMPT_COMMAND="${PROMPT_COMMAND//_agent_resurrect_first_prompt; /}"
  PROMPT_COMMAND="${PROMPT_COMMAND//_agent_resurrect_first_prompt/:}"
  [ "$PROMPT_COMMAND" = ":" ] && PROMPT_COMMAND=""
  local key=""
  [ -n "${ITERM_SESSION_ID:-}" ] && key="iterm-${ITERM_SESSION_ID#*:}"
  [ -n "$key" ] && [ -f "${XDG_STATE_HOME:-$HOME/.local/state}/agent-resurrect/panes/$key.json" ] || return 0
  "${AGENT_RESURRECT_BIN:-$HOME/.local/bin/agent-resurrect}" auto
}
PROMPT_COMMAND="_agent_resurrect_first_prompt${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
