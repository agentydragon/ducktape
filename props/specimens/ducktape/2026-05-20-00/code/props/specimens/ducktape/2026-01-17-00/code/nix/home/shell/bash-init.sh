# Bash-specific initialization
# Loaded by home-manager's programs.bash.initExtra

# Disable flow control (Ctrl-S/Q)
stty -ixon

# Set up prompt with color support and terminal title
_setup_bash_prompt() {
  local debian_chroot=""
  [ -z "${debian_chroot:-}" ] && [ -r /etc/debian_chroot ] && debian_chroot=$(cat /etc/debian_chroot)

  # Detect color support
  case "$TERM" in
    xterm-color | *-256color)
      PS1='${debian_chroot:+($debian_chroot)}\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '
      ;;
    *)
      PS1='${debian_chroot:+($debian_chroot)}\u@\h:\w\$ '
      ;;
  esac

  # Set xterm title
  case "$TERM" in
    xterm* | rxvt*)
      PS1="\[\e]0;${debian_chroot:+($debian_chroot)}\u@\h: \w\a\]$PS1"
      ;;
  esac
}
_setup_bash_prompt

# Custom completions (bash-completion itself is enabled via programs.bash.enableCompletion)
complete -cf sudo
complete -cf man

# Colored man pages (zsh has oh-my-zsh plugin for this)
man() {
  env \
    LESS_TERMCAP_mb=$(printf "\e[1;31m") \
    LESS_TERMCAP_md=$(printf "\e[1;31m") \
    LESS_TERMCAP_me=$(printf "\e[0m") \
    LESS_TERMCAP_se=$(printf "\e[0m") \
    LESS_TERMCAP_so=$(printf "\e[1;44;33m") \
    LESS_TERMCAP_ue=$(printf "\e[0m") \
    LESS_TERMCAP_us=$(printf "\e[1;32m") \
    man "$@"
}
