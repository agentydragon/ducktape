# TODO — Agentplane runner

## Check whether an interrupted-turn test depends on when Claude flushes its transcript

`harness_tests/claude/test_turns.py::test_resume_after_an_interrupted_partial_turn_replays_only_completed_model_history`
interrupts a partial turn, kills the harness right after the result, and sees neither the partial
text nor the thinking replayed on resume. A probe found while fixing the Claude reasoning
reconciliation (#9178), which let the transcript flush before the kill, saw the interrupted turn's
thinking and partial text replayed on resume. Not investigated.

Find out whether the test passes only because of that timing, and whether `claude.py`'s reconcile
and `claude_history.py` must then handle the flushed case. On an ordinary (non-resumed) interrupt,
`reconcile` already reports a completed thinking block as retained even where Claude drops it.
