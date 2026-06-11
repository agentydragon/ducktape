# Zsh-specific initialization
# Loaded by home-manager's programs.zsh.initExtra

# Prompt selection: USE_OHMYPOSH=1 for Oh-My-Posh, otherwise Powerlevel10k
if [[ -n "$USE_OHMYPOSH" ]]; then
	# Oh-My-Posh prompt - clear zsh RPROMPT to avoid conflict
	unset RPROMPT
	eval "$(oh-my-posh init zsh --config ~/.config/oh-my-posh/config.json)"
else
	# Powerlevel10k instant prompt (must be near top of .zshrc)
	if [[ -r "${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-$USER.zsh" ]]; then
		source "${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-$USER.zsh"
	fi
	# Powerlevel10k configuration
	[[ -f ~/.p10k.zsh ]] && source ~/.p10k.zsh
fi

# Disable menu completion
unsetopt menu_complete
unsetopt auto_menu

# Completion configuration
zstyle ':completion:*' completer _complete _ignored
zstyle ':completion:*' group-name ''
zstyle ':completion:*' matcher-list ''

# Custom keybindings for zsh-autosuggestions
accept_or_end() {
	if (( ${+widgets[autosuggest-accept]} )) && [[ -n "$POSTDISPLAY" ]]; then
		zle autosuggest-accept
		zle reset-prompt
		return
	fi
	zle end-of-line
}
zle -N accept-or-end accept_or_end
bindkey -M emacs '^E' accept-or-end
bindkey -M viins '^E' accept-or-end
ZSH_AUTOSUGGEST_ACCEPT_WIDGETS+=(accept-or-end)
# Ctrl+F: Accept suggestion word-by-word (forward-word is in ZSH_AUTOSUGGEST_PARTIAL_ACCEPT_WIDGETS)
bindkey '^F' forward-word
