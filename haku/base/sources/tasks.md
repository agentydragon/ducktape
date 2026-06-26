# Google Tasks

Scan Google Tasks for overdue and stale to-dos with the read-only Google token
(the read-only Google token `$TOK`, see [README](README.md)). List your task lists, then
each list's open tasks:
`curl -s -H "Authorization: Bearer $TOK" 'https://tasks.googleapis.com/tasks/v1/users/@me/lists'`,
then `.../lists/{tasklistId}/tasks?showCompleted=false&showHidden=false`. Look for:

- tasks past their `due` date (overdue)
- tasks untouched for a long time (`updated` far in the past) — stale, worth
  doing, rescheduling, or dropping
- vague one-line captures that imply a real next step
- things already done elsewhere that can just be closed

A long tail of stale entries is itself a finding — prefer one item proposing a
cleanup pass over one item per cruft task. File items referencing the task by
title + list + `due`/`updated`. Needs the `tasks.readonly` scope; if a call
returns 403, the scope isn't granted — note the gap in your log and move on.
