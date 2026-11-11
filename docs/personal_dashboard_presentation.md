# Personal dashboard presentation

Presentation expectations for AIQuota and Plaid Spend on GNOME, CLI, web and tmux. These are
targets for future changes; existing surfaces may not yet follow every rule.

## Information and layout

- Lead with the information needed for the next decision. Show each fact once in a view:
  a status sentence does not repeat a visible balance, exhausted meter or extra-spend amount.
  A compact signal is useful when it adds meaning or remains visible while its details are hidden.
- Group related values and label them consistently. Use the same term for equivalent concepts
  across providers and surfaces; retain different names for genuinely different pools, units,
  scopes or states. Keep provider and organization attribution when it matters.
- Put infrequent details behind an explicit expansion or in the larger web view. Default GNOME
  popups should be compact and scannable, with short labels and few rows; do not trade away
  legibility or useful context to save a line. The panel and tmux can be shorter summaries.
- Preserve distinctions that change interpretation or action: current versus historical,
  available versus in use, observed versus inferred, stale versus fresh, unknown versus zero,
  and pending reset versus refilled. Show errors, uncertainty and freshness where relevant.
  Never rely on color alone to convey a state.
- Keep equivalent facts, terminology, formatting and state semantics aligned across surfaces.
  More space can reveal more detail, but must not change what the same information means.

## Dates and amounts

- Render instants in the viewer's local time zone. Omit its name in normal views; show the zone
  or offset in an explicitly requested exact timestamp when ambiguity matters. Preserve date-only
  values as calendar dates rather than converting them as instants; respect a report's defined
  business time zone when identifying its reporting days.
- In normal views, include the year only when it differs from the viewer's current year. Include
  the time of day for instants within 14 days of now; otherwise show the date. Use a relative
  age or countdown when it answers the question better. An exact timestamp can be available in
  details when useful, using the same policy across providers and surfaces.
- For summary amounts, show fractional currency units only for nonzero amounts below 10 major
  units; otherwise round to whole units. Respect each currency's precision. Exact transaction
  amounts and other values where the fraction affects a decision retain their full precision in
  the relevant detail view. Rounding must not turn a nonzero amount into zero or obscure a sign,
  threshold crossing or material difference between values.
- Share formatters where practical, and test the same examples and boundaries across languages
  and surfaces: year changes, the 14-day cutoff, daylight-saving transitions, small and negative
  amounts, zero, and currencies with different minor-unit precision.

## Review checklist

- Does every default row add a fact or an actionable distinction? Can related information be
  combined without making it ambiguous?
- Are labels and precision consistent with equivalent facts elsewhere? Can a reader identify
  the provider, organization, currency, time zone context and freshness when needed?
- Are unknown, stale, error and boundary cases still understandable? Is the compact view legible
  without color, and is fuller information available where it matters?
- Review screenshots of normal, crowded, expanded and exceptional states for both GNOME popups;
  compare corresponding CLI and web output and the intentionally shorter tmux/panel summary.
