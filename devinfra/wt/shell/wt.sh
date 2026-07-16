# shellcheck shell=bash
# wt shell function definition. Sourced into interactive shells.
# __PY__ will be substituted at install-print time with the Python executable.

wt() {
  local wt_command_file
  wt_command_file=$(mktemp)
  trap 'rm -f "$wt_command_file"' EXIT

  __PY__ -m wt.cli sh "$@" 3>"$wt_command_file"
  local wt_exit_code=$?

  if [ $wt_exit_code -eq 0 ] || [ $wt_exit_code -eq 2 ]; then
    if [ -s "$wt_command_file" ]; then
      # shellcheck disable=SC2155
      local wt_shell_commands="$(cat "$wt_command_file")"
      if [ -n "$wt_shell_commands" ]; then
        eval "$wt_shell_commands"
      fi
    fi
  fi

  rm -f "$wt_command_file"
  return $wt_exit_code
}
