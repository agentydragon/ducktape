# Prompt optimization experiments

Retired experiments for improving agent prompts by replaying tasks and comparing
outputs:

- **Feedback replay (sysrw):** collect conversation turns where the user flagged an
  assistant mistake, preserving the preceding context and corrective feedback.
  Replay those cases offline with alternative system prompts, compare the new
  responses against the user's feedback, and select prompts that avoid the
  original failures.
- **LLM prompt optimizer (INOP):** run an agent on tasks with a candidate prompt
  and score its outputs. Give the prompt, outputs and scores to another LLM,
  which proposes a revised prompt intended to improve the scores. Repeat the
  run–score–revise loop and compare candidates to select a better prompt.
