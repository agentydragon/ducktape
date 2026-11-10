# shellcheck shell=bash
# wt shell function definition. Sourced into interactive shells.
# __PY__ will be substituted at install-print time with the Python executable.

wt() {
  local wt_shell_commands wt_exit_code
  # Capture fd3 commands while forwarding normal stdout through a scoped fd4.
  {
    if wt_shell_commands=$(__PY__ -m x.wt.cli sh "$@" 3>&1 1>&4); then
      wt_exit_code=0
    else
      wt_exit_code=$?
    fi
  } 4>&1

  if [ $wt_exit_code -eq 0 ] || [ $wt_exit_code -eq 2 ]; then
    if [ -n "$wt_shell_commands" ]; then
      eval "$wt_shell_commands"
    fi
  fi

  return $wt_exit_code
}
