package store

import "context"

const SessionNotifyChannel = "agentplane_dispatcher_session"

func (s *Store) SessionsAfter(ctx context.Context, version int64, caller Caller) ([]*Session, error) {
	scope, args := OwnerScope(caller, "", 2)
	return s.querySessions(ctx, "SELECT "+sessionColumns+
		" FROM session WHERE version > $1 AND "+scope+" ORDER BY version LIMIT 100", append([]any{version}, args...)...)
}
