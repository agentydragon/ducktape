package auth

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/internal/auth/session"
)

type LoginTokens struct {
	AccessToken  string
	ExpiresAt    time.Time
	RefreshToken string
}

type OperatorSession struct {
	Issuer   string
	Subject  string
	Username string
	Tokens   *LoginTokens
}

type SessionRow struct {
	store *session.Store
	id    session.ID
}

// ID returns the one-way database key used by UntilEnded.
func (r *SessionRow) ID() string { return string(r.id) }

// Login returns the current login after an expiry-aware read.
func (r *SessionRow) Login(ctx context.Context) (*OperatorSession, error) {
	record, err := r.store.Read(ctx, r.id)
	if err != nil || record == nil {
		return nil, err
	}
	return operatorSession(record.Login), nil
}

// WithLockedLogin serializes token refresh and replacement against session termination.
func (r *SessionRow) WithLockedLogin(ctx context.Context, update func(*OperatorSession) (*OperatorSession, error)) error {
	return r.store.RefreshLogin(ctx, r.id, func(current *session.Login) (*session.Login, error) {
		updated, err := update(operatorSession(current))
		return persistedLogin(updated), err
	})
}

func operatorSession(login *session.Login) *OperatorSession {
	if login == nil {
		return nil
	}
	result := &OperatorSession{Issuer: login.Issuer, Subject: login.Subject, Username: login.Username}
	if login.Tokens != nil {
		result.Tokens = &LoginTokens{
			AccessToken: login.Tokens.AccessToken, ExpiresAt: login.Tokens.ExpiresAt,
			RefreshToken: login.Tokens.RefreshToken,
		}
	}
	return result
}

func persistedLogin(login *OperatorSession) *session.Login {
	if login == nil {
		return nil
	}
	result := &session.Login{Issuer: login.Issuer, Subject: login.Subject, Username: login.Username}
	if login.Tokens != nil {
		result.Tokens = &session.Tokens{
			AccessToken: login.Tokens.AccessToken, ExpiresAt: login.Tokens.ExpiresAt,
			RefreshToken: login.Tokens.RefreshToken,
		}
	}
	return result
}

func persistedAttempts(attempts map[string]loginAttempt) map[string]session.Attempt {
	persisted := make(map[string]session.Attempt, len(attempts))
	for state, attempt := range attempts {
		persisted[state] = session.Attempt{
			Nonce: attempt.Nonce, CodeVerifier: attempt.CodeVerifier, ExpiresAt: attempt.ExpiresAt,
		}
	}
	return persisted
}

func operatorAttempts(attempts map[string]session.Attempt) map[string]loginAttempt {
	projected := make(map[string]loginAttempt, len(attempts))
	for state, attempt := range attempts {
		projected[state] = loginAttempt{
			Nonce: attempt.Nonce, CodeVerifier: attempt.CodeVerifier, ExpiresAt: attempt.ExpiresAt,
		}
	}
	return projected
}
