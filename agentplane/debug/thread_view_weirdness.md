# Thread disclosure review, October 2026

[PR #10874](https://github.com/agentydragon/ducktape/pull/10874) addressed this review:
collapsed hover shape, summary position on opening, nested sticky heading layout,
sticky command identity, returning long output to its preview, tool evidence controls, and user-message
diagnostics below the bubble. Shell metadata moved above the command and a single
Raw switch now covers arguments and output. The
[current disclosure design](../app/frontend/sessions/README.md#disclosure-layout-ownership)
owns layout policy. The [history layout tests](../app/frontend/test_visual_history_layout.py)
cover header alignment and collapse on desktop and phone, and the
[history visual tests](../app/frontend/test_visual_history.py) cover sticky headings,
tool inspection, and Raw.

## Historical screenshots

The original phone and desktop image URLs returned HTTP 404 when rechecked.
The notes below refer to [the reviewed candidate](https://github.com/agentydragon/ducktape/commit/6ae12ede7e14a2ffe93c8e0fac2b6d90f4874c96).

- [Phone](https://s3.allegedly.works/pr-visuals/commits/6ae12ede7e14a2ffe93c8e0fac2b6d90f4874c96/tests/agentplane-app-test-thread-browser-6b162a4490/thread-command-collapsed-phone.png).
  Raw and the Bash editor crowded the right edge, and separator lines followed nested
  insets. The operator was open to removing the script border. The shipped command
  uses a borderless editor and preserves the right gutter. [PR #10909](https://github.com/agentydragon/ducktape/pull/10909)
  landed a shallower label indent and a separator only below the outer expanded heading.
- [Desktop](https://s3.allegedly.works/pr-visuals/commits/6ae12ede7e14a2ffe93c8e0fac2b6d90f4874c96/tests/agentplane-app-test-thread-browser-6b162a4490/thread-command-collapsed-desktop.png).
  The command preview looked off-center and the outer summary was shorter than the
  open tool summary. The operator accepted equal heights or a taller outer summary.
  The single-line expanded headers now use equal heights and centered text.

## Remaining feedback

- Output counts source lines. A long line can wrap to several screen lines on a
  phone, so the count can understate the height. Whether the label should convey
  the rendered height is open.
- The operator deferred stronger failed-call indicators because a nonzero exit
  does not necessarily signal trouble.
- The operator deferred polishing observation/raw-frame layout, including narrow
  wrapping. This is separate from moving user-message diagnostics below the bubble.
