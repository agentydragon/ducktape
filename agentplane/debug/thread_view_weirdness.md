# Thread view weirdness

Observed UI symptoms and user review decisions, without diagnosis.

## Collapsed disclosures

- When hovering over a collapsed run or shell tool call in thread history, the
  highlighted shape has rounded top corners and square bottom corners.

## Opening a run

- As a run opens, its summary text shifts right and its chevron shifts left,
  although the summary row remains full-width.

## Runner-protocol inspection affordance

- Reasoning and assistant-text items show a magnifying-glass affordance on hover
  to inspect runner-protocol details; at least Bash tool calls appear not to show
  it.

## Long tool output

- After expanding clipped output with “Show all,” there is no control to return
  to the clipped preview; collapsing and reopening the whole Output section leaves
  the output fully expanded.

## Nested sticky headers

- In a sticky stack of run, shell-call, and output headers, text sizes go small,
  large, small.
- The open run's horizontal padding offsets the shell call on both sides. The
  Output label starts at the same left position as the Shell label instead of
  being inset within the shell call.
- Despite that shared left position, the Output chevron sits farther right than
  the shell-call chevron.
- The horizontal gap between the run and shell-call chevrons is much larger than
  the gap between the shell-call and output chevrons.
- With all three headers sticky, their repeated left- and right-side lines and
  controls make the stack visually busy.

## User-message inspection

- In dark mode, inspection diagnostics render dark-grey backgrounds inside the
  user message's blue bubble. The nested panels look as though their background
  belongs to the page rather than to the bubble (user-reported).

## Shell-call metadata

- “Run by /bin/bash -lc” and the working-directory path occupy separate lines
  below the command. The command invocation and path use proportional text.
- The Raw switch occupies a separate row above the command, leaving space beside
  it while execution metadata occupies space below the command.

## Output boundaries

- The output code block has its own border inside the surrounding disclosure
  and tool-call boundaries, repeating borders around the same content.

## Standalone reasoning

- A standalone reasoning item's heading is larger than a run summary.

## Additional review observations

- An expanded shell call's sticky heading shows only “Shell”; the command preview
  disappears on expansion. While reading scrolled output, multiple different
  shell calls have indistinguishable headings.
- On mobile, output wraps source lines into several screen lines. The displayed
  line count describes source lines, which can substantially understate the
  vertical amount of output shown.
- The shell call's “Raw” switch does not name its scope: it changes the command
  arguments while leaving the output presentation unchanged (source inspection).

## Review of candidate `6ae12ede7e`

Observed in commit `6ae12ede7e14a2ffe93c8e0fac2b6d90f4874c96`, in
[`thread-command-collapsed-phone.png`](https://s3.allegedly.works/pr-visuals/commits/6ae12ede7e14a2ffe93c8e0fac2b6d90f4874c96/tests/agentplane-app-test-thread-browser-6b162a4490/thread-command-collapsed-phone.png):

- The Raw switch hugs the outer box's right edge, without a comfortable right
  inset.
- The nested horizontal separator lines stop short of the outer box's left
  border. Their inset increases with nesting. Whether the separators should
  follow the label indentation is unresolved.
- The Bash script block's right border touches the outer box's right border,
  making that edge look thicker.
- The user is open to removing the border around the Bash script text; this is
  an option, not a selected solution.

Also observed in the same commit, in
[`thread-command-collapsed-desktop.png`](https://s3.allegedly.works/pr-visuals/commits/6ae12ede7e14a2ffe93c8e0fac2b6d90f4874c96/tests/agentplane-app-test-thread-browser-6b162a4490/thread-command-collapsed-desktop.png):

- The vertical space above “Run tool a” differs from the space below it; the
  text does not look vertically centered in its summary block.
- The outer “3 tool calls” summary block is shorter than the nested “Bash Run
  tool a” summary block.
- Equal-height outer and nested summaries are acceptable. If their heights
  differ, the outer summary should be taller, not shorter. Neither treatment
  has been selected.

## User review decisions

- Keeping command identity or a summary visible while an expanded shell call's
  heading is sticky is desirable.
- Raw viewing should cover both arguments and output. Either one shared switch
  or separate switches is acceptable; neither arrangement has been selected.
- Leave failed-call indicators as they are for now. An agent's nonzero command
  exit is not necessarily a sign of trouble.
- Render user-message inspection below the message bubble at the thread's width,
  rather than nesting diagnostics inside the bubble.
- Defer polishing the observation/raw-frame layout, including narrow wrapping.
  That is separate from the reported dark-mode background mismatch.
