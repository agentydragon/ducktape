# gmail_triage (example)

Read Gmail with the read-only token (see `../instructions.md` → _Hard rules_). List new
mail since your bookmark (on the first run, a window like `newer_than:7d`):
`curl -s -H "Authorization: Bearer $TOK" 'https://gmail.googleapis.com/gmail/v1/users/me/messages?q=newer_than:7d'`,
then fetch each with `.../messages/{id}?format=metadata` (use `format=full` only
when you must read a body to judge it). Useful `q=` filters: `is:unread`,
`is:important`, `category:primary`. Look for:

- threads awaiting a reply from the operator (they're the last non-operator
  participant, or a question is directed at them)
- deadlines / dated asks buried in mail (RSVPs, payments due, document requests)
- subscriptions, renewals, or price-increase notices worth cancelling
- security / account alerts not yet acted on

File one item per finding, referencing the thread by subject + sender + date. For
actionable ones, write a `prepared_prompt` for an executor session (which has
write access) to draft the reply / cancel / RSVP.
