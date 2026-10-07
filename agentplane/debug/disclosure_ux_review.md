# Disclosure layout review

Chromium review at devel `3fbd3297d4`, with staging-derived geometry fixtures and
an operator-supplied 836×896 dark-theme screenshot. The findings below describe that baseline. This branch now implements the
approved changes: aligned step labels, centered chevrons, no singleton item rules,
one panel/card inset per level, and controls that cover their card edges. The
implementation includes the hover changes originally proposed in PR #9393.

Permanent captures include light/dark desktop overviews and expanded runs, phone
views, expanded calls, and scrolled output. Their PNGs are published by the PR
visual-review bot; they are not checked-in pixel baselines. The expanded-run test
also asserts that plain and expandable step labels share the same left edge.

## Findings

- The supplied screenshot's horizontal rules span image pixels x=32..804:
  32 pixels of background on the left and 31 on the right. Prose and the composer
  begin around x=20, while disclosure labels begin around x=52. The rules are
  symmetric within pixel rounding, but these competing insets make the layout
  inconsistent. Image pixels here are not necessarily CSS pixels.
- An expandable step title starts 16 CSS pixels after a plain reasoning title.
  `Accordion.Control` supplies leading padding that `.agentplane-step-static`
  does not share (`threads/projected_session.css`).
- The chevron center is about 3.5 CSS pixels above the title text center on desktop
  and mobile. The outer control uses baseline alignment; title/preview alignment
  already has its own inner flex row.
- The enclosing run's `Accordion.Item` draws a bottom rule 13 CSS pixels above
  its Paper frame. Nested tools and outputs each add another item rule. This
  produces the extra wide interior line at the end of a run and inconsistent
  separators beside plain reasoning rows. `disclosure.tsx` creates singleton
  accordion items; `disclosure.css` retains their default bottom border.
- Card padding, panel padding, and row gaps accumulate. Phone output captures
  make the resulting loss of useful text width particularly visible. Spacing
  outside a control also remains outside its hover fill.
- Both staging-derived fixtures have symmetric outer gutters at desktop/mobile
  sizes: 16 CSS pixels per side, with history `scrollWidth == clientWidth` and
  `scrollLeft == 0`. No missing right gutter or horizontal overflow was reproduced.
  The staging CSS served during the review has the same relevant shell padding
  and step-control alignment rules as this revision.

## Approved changes

1. Give interactive and plain steps one text origin. Center the outer chevron
   while retaining baseline alignment inside the title/preview row.
2. Remove default singleton accordion item borders. The desktop/mobile comparison
   captures look cleaner without them, while the expanded run frame and sticky
   heading divider still provide grouping. Check standalone disclosures too before
   shipping this policy; the comparison override is deliberately broader than a
   final selector might be.
3. Give each nesting level one owner for horizontal padding. Reduce stacked
   card/panel insets, preserve mobile hit areas, and make the row's hover control
   cover its own spacing. This shares the card-edge variables from PR #9393.

The screenshots do not justify a speculative width or scrollbar-gutter change.
Removing borders alone leaves the label alignment and accumulated padding issues.

## Fixture provenance and limits

The completed diagnostic fixture has 59 items across four turns: 4 inputs,
24 reasoning items, 27 command calls, and 4 assistant messages. Most calls are
read/search operations. The reported-thread excerpt has 30 items: 1 input,
12 reasoning items, 16 calls, and 1 assistant message. One call is failed context
compaction; the others are commands. This excerpt is not proven to be the same
14-call/10-reasoning group originally reported by the operator.

Only event kinds/order, completion/failure flags, and text size/line-count metadata
were exported. Fixture prose, arguments, and outputs are newly authored; repeated
safe text preserves approximate layout geometry. These are not verbatim transcripts
or evidence of semantic realism in the replacement commands. Both excerpts contain
completed items, even though the reported thread also had an active feed.

Python owns all interaction and capture steps. Four fixture entries reuse the
existing desktop/mobile viewport presets. Sixteen captures cover overview,
expanded run start/end, expanded call, and scrolling through long output. Geometry
JSON is emitted with the screenshots. The border-removal variants use temporary
Playwright CSS injection and are not permanent test behavior.

## Baseline comparison validation

All 13 permanent capture cases passed across the full run and focused rerun:

- Full run: `ab60e78c-1dc7-49d4-9e33-5def07ea92e1`. The mobile output driver
  initially targeted a virtualized CodeMirror line that was not mounted; it now
  scrolls by output geometry and asserts the output heading remains visible.
- Focused rerun: `71617482-ec82-4d84-ba37-7d0650d0c6c6`, all six cases passed
  (four call/output states and two temporary border comparisons).

A separate two-case dark-theme comparison at 960×900 passed in invocation
`4cc8d6c3-cc8e-46b3-82a1-ccd0744e8d48`. This temporary viewport override is
not a new shared preset.

Screenshots were inspected. These tests assert readiness and interaction behavior;
visual appearance remains a human review, not a pixel-baseline assertion.
