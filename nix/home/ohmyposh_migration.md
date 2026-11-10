# Oh-My-Posh cache hypothesis

Updated 2026-10-28. Oh-My-Posh remains an opt-in prompt; the current zsh
initialization defaults to Powerlevel10k in [`shell/zsh-init.zsh`](shell/zsh-init.zsh).
This note tracks the unresolved cache report only.

The original investigation reported that a session cache key appeared to use
`CONFIG` without the config path. It suspected this could make a shell inheriting
`POSH_SESSION_ID` reuse another shell's config. That source inspection was from a
local checkout of Oh-My-Posh; the behavior has not been checked against the
currently pinned package or reproduced with the current configuration.

The earlier investigation also suggested clearing `~/.cache/oh-my-posh`, but the
repository does not do this automatically and there is no recorded verification
that clearing it fixes the report. Reproduce the issue with `USE_OHMYPOSH=1` before
choosing a fix.
