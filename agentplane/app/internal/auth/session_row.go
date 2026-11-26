package auth

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/app/internal/auth/session"
)

type SessionRow struct {
	store *session.Store
	id    session.ID
}

// ID returns the one-way database key used by UntilEnded.
func (r *SessionRow) ID() string { return string(r.id) }

// Login returns the current login after an expiry-aware read.
func (r *SessionRow) Login(ctx context.Context) (*session.Login, error) {
	record, err := r.store.Read(ctx, r.id)
	if err != nil || record == nil {
		return nil, err
	}
	return record.Login, nil
}

// WithLockedLogin serializes token refresh and replacement against session termination.
func (r *SessionRow) WithLockedLogin(ctx context.Context, update func(*session.Login) (*session.Login, error)) error {
	return r.store.RefreshLogin(ctx, r.id, update)
}
