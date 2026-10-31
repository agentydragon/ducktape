# History read and follow consumers

The implemented raw-entry API and its cursor, correction and renewal contract are in the
[History Service README](../history_service/README.md#read-and-follow-one-session).
The browser stays on Electric; a browser facade remains separate work.

## Remaining consumers and decisions

- Move the Notification Service's outcome scan only if `NOTIFICATION_COMMAND_OUTCOME_DESIGN` still
  requires History reads. The app's raw debug reads stay on `ReadSessionEvents`.
- Decide whether `ReadSessionEvents` can later become `FollowSession` with `follow = false`, once
  its callers need nothing else from it.
