# Partial run with the canonical `bash_session` solver

Killed at 30/100 scored. Preserved here as evidence behind the
scaffold change documented in <../../README.md>: `gpt-oss:20b` was
issuing `action: "type"` (no `type_submit`) into the TTY tool,
leaving the shell waiting on input and burning the message budget on
heredoc thrash. Headline: 4/30 = 0.133 before kill.

The current run uses `swebench_react_task.py@swe_bench_react`
(stateless `bash` + `python` + `think`) — see the parent README.
