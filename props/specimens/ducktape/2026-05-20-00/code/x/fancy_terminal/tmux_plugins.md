# Tmux plugin landscape (quick guide)

Use TPM (tmux plugin manager) to install: <https://github.com/tmux-plugins/tpm>

Setup once:

```
git clone https://github.com/tmux-plugins/tpm ~/.tmux/plugins/tpm
# In ~/.tmux.conf
set -g @plugin 'tmux-plugins/tpm'
run '~/.tmux/plugins/tpm/tpm'
# Reload (prefix r), then press prefix I to install listed plugins
```

Categories and solid picks

- Session/save/restore
  - tmux-plugins/tmux-resurrect — save/restore sessions, panes, layouts
  - tmux-plugins/tmux-continuum — autosave and restore on start
- Clipboard & copy-mode
  - tmux-plugins/tmux-yank — copy to system clipboard (macOS/Linux)
  - tmux-plugins/tmux-copycat — regex searching in copy-mode (URLs, files, hashes)
- Navigation & pane movement
  - christoomey/vim-tmux-navigator — seamless hjkl moves between Vim and tmux
  - fkalis/tmux-fingers — jump to paths/URLs with key hints
  - eddieantonio/tmux-sensible — a good baseline defaults pack
- Fuzzy finders & pickers
  - sainnhe/tmux-fzf — fzf pickers for sessions/windows/panes
  - thewtex/tmux-mem-cpu-load — quick CPU/mem info segment (lightweight)
- Status bar & visual indicators
  - tmux-plugins/tmux-prefix-highlight — show when prefix is active
  - erikw/tmux-powerline or reobin/tmux-pokemon (fun) — themed status bars
- System integrations & notifications
  - joshmedeski/tmux-nerd-font-window-name — icons for window names
  - mkrrr/tmux-notify — desktop notifications for long tasks
- Quality of life
  - tmux-plugins/tmux-pain-control — easy pane resizing/moving
  - tmux-plugins/tmux-open — open highlighted selection with OS default
  - tmux-plugins/tmux-urlview — view/click URLs from panes

Recommended minimal set

```
set -g @plugin 'tmux-plugins/tpm'
set -g @plugin 'tmux-plugins/tmux-resurrect'
set -g @plugin 'tmux-plugins/tmux-continuum'
set -g @plugin 'tmux-plugins/tmux-yank'
set -g @plugin 'tmux-plugins/tmux-prefix-highlight'
# Optional: vim integration
set -g @plugin 'christoomey/vim-tmux-navigator'

# Settings
set -g @continuum-restore 'on'
set -g @resurrect-strategy-nvim 'session'
set -g @resurrect-strategy-vim  'session'
```

Notes

- Prefix defaults to Ctrl-b; if you change it, most plugins just work but some keyhints assume the default.
- Resurrect can restore panes and commands; for long-running servers, consider supervising via your usual tools rather than restore-on-start.
- If a plugin misbehaves, comment it out, reload (prefix r), then prefix Alt-u to uninstall via TPM.

Troubleshooting

- Verify TPM loaded: prefix I should show an install log.
- Check plugin paths: ~/.tmux/plugins/<name>.
- Reload conf: tmux source-file ~/.tmux.conf (or bind r like in our config).
