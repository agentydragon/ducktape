# History read and follow consumers

The implemented raw-entry API and its cursor, correction and renewal contract are in the
[History Service README](../history_service/README.md#read-and-follow-one-session).
The browser reads snapshots through the generated History facade and follows its Thread view through Electric.

## Remaining consumers and decisions

- Move the Notification Service's outcome scan only if `NOTIFICATION_COMMAND_OUTCOME_DESIGN` still
  requires History reads. The app's raw debug view also uses History's observation and native-frame
  reads alongside `ReadSessionEvents`.
- Decide whether `ReadSessionEvents` can later become `FollowSession` with `follow = false`, once
  its callers need nothing else from it.
