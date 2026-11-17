package store

import (
	"context"
)

const SessionNotifyChannel = "agentplane_dispatcher_session"

func (s *Store) SessionsAfter(ctx context.Context, version int64, caller Caller) ([]*Session, error) {
	scope, args := OwnerScope(caller, "", 2)
	rows, err := s.DB.QueryContext(ctx, "SELECT "+sessionColumns+
		" FROM session WHERE version > $1 AND "+scope+" ORDER BY version LIMIT 100", append([]any{version}, args...)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var sessions []*Session
	for rows.Next() {
		session, err := scanSession(rows)
		if err != nil {
			return nil, err
		}
		sessions = append(sessions, session)
	}
	return sessions, rows.Err()
}
