package auth

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

const SessionUpdatesChannel = "agentplane_operator_sessions"

const sessionNotifyChannel = SessionUpdatesChannel

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

type browserRow struct {
	id                string
	expiresAt         time.Time
	absoluteExpiresAt time.Time
	payload           map[string]any
	login             *OperatorSession
}

type sessionStore struct {
	db   *sql.DB
	idle time.Duration
	step time.Duration
	now  func() time.Time
}

func rowID(handle string) string {
	sum := sha256.Sum256([]byte(handle))
	return hex.EncodeToString(sum[:])
}

func newHandle() (string, error) {
	bytes := make([]byte, 32)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(bytes), nil
}

func (s *sessionStore) read(ctx context.Context, id string) (*browserRow, error) {
	row, err := scanBrowserRow(s.db.QueryRowContext(ctx, `
		SELECT id, expires_at, absolute_expires_at, payload::text,
		       operator_issuer, operator_subject, operator_username,
		       access_token, access_token_expires_at, refresh_token
		FROM operator_browser_session WHERE id = $1`, id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	now := s.now().UTC()
	if !row.expiresAt.After(now) {
		tx, beginErr := s.db.BeginTx(ctx, nil)
		if beginErr != nil {
			return nil, beginErr
		}
		defer tx.Rollback()
		var deleted string
		err = tx.QueryRowContext(ctx, `
			WITH candidate AS (
				SELECT id FROM operator_browser_session WHERE id = $1 AND expires_at <= $2
				FOR UPDATE SKIP LOCKED
			)
			DELETE FROM operator_browser_session WHERE id IN (SELECT id FROM candidate) RETURNING id`, id, now).Scan(&deleted)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		if err == nil {
			if err := notifySessions(ctx, tx); err != nil {
				return nil, err
			}
		}
		if err := tx.Commit(); err != nil {
			return nil, err
		}
		return nil, nil
	}
	deadline := now.Add(s.idle)
	if deadline.After(row.absoluteExpiresAt) {
		deadline = row.absoluteExpiresAt
	}
	step := s.step
	if half := s.idle / 2; half < step {
		step = half
	}
	if deadline.Sub(row.expiresAt) > step {
		var result sql.Result
		result, err = s.db.ExecContext(ctx, `
			WITH candidate AS (
				SELECT id FROM operator_browser_session WHERE id = $1 AND expires_at < $2
				FOR UPDATE SKIP LOCKED
			)
			UPDATE operator_browser_session SET expires_at = $2
			WHERE id IN (SELECT id FROM candidate)`, id, deadline)
		if err != nil {
			return nil, err
		}
		if count, countErr := result.RowsAffected(); countErr == nil && count > 0 {
			row.expiresAt = deadline
		}
	}
	return row, nil
}

func scanBrowserRow(scanner interface{ Scan(...any) error }) (*browserRow, error) {
	var row browserRow
	var payload string
	var issuer, subject, username, accessToken, refreshToken sql.NullString
	var tokenExpiry sql.NullTime
	if err := scanner.Scan(&row.id, &row.expiresAt, &row.absoluteExpiresAt, &payload,
		&issuer, &subject, &username, &accessToken, &tokenExpiry, &refreshToken); err != nil {
		return nil, err
	}
	if err := json.Unmarshal([]byte(payload), &row.payload); err != nil {
		return nil, fmt.Errorf("decode browser session payload: %w", err)
	}
	if row.payload == nil {
		row.payload = map[string]any{}
	}
	if issuer.Valid {
		row.login = &OperatorSession{Issuer: issuer.String, Subject: subject.String, Username: username.String}
		if accessToken.Valid {
			row.login.Tokens = &LoginTokens{AccessToken: accessToken.String, ExpiresAt: tokenExpiry.Time}
			if refreshToken.Valid {
				row.login.Tokens.RefreshToken = refreshToken.String
			}
		}
	}
	return &row, nil
}

func (s *sessionStore) create(ctx context.Context, payload map[string]any, absolute time.Time) (string, error) {
	handle, err := newHandle()
	if err != nil {
		return "", err
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	deadline := s.now().UTC().Add(s.idle)
	if deadline.After(absolute) {
		deadline = absolute
	}
	_, err = s.db.ExecContext(ctx, `INSERT INTO operator_browser_session
		(id, expires_at, absolute_expires_at, payload) VALUES ($1, $2, $3, $4::jsonb)`,
		rowID(handle), deadline, absolute, string(data))
	if err != nil {
		return "", err
	}
	return handle, nil
}

func (s *sessionStore) logIn(ctx context.Context, pendingID string, payload map[string]any, login OperatorSession, absolute time.Time) (string, bool, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return "", false, err
	}
	defer tx.Rollback()
	if pendingID != "" {
		result, err := tx.ExecContext(ctx, `DELETE FROM operator_browser_session WHERE id = $1`, pendingID)
		if err != nil {
			return "", false, err
		}
		count, err := result.RowsAffected()
		if err != nil {
			return "", false, err
		}
		if count == 0 {
			return "", false, nil
		}
	}
	handle, err := newHandle()
	if err != nil {
		return "", false, err
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return "", false, err
	}
	deadline := s.now().UTC().Add(s.idle)
	if deadline.After(absolute) {
		deadline = absolute
	}
	var accessToken, refreshToken any
	var tokenExpiry any
	if login.Tokens != nil {
		accessToken, tokenExpiry = login.Tokens.AccessToken, login.Tokens.ExpiresAt
		if login.Tokens.RefreshToken != "" {
			refreshToken = login.Tokens.RefreshToken
		}
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO operator_browser_session
		(id, expires_at, absolute_expires_at, payload, operator_issuer, operator_subject,
		 operator_username, access_token, access_token_expires_at, refresh_token)
		VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10)`, rowID(handle), deadline, absolute,
		string(data), login.Issuer, login.Subject, login.Username, accessToken, tokenExpiry, refreshToken)
	if err != nil {
		return "", false, err
	}
	_, err = tx.ExecContext(ctx, `DELETE FROM operator_browser_session WHERE expires_at <= $1`, s.now().UTC())
	if err != nil {
		return "", false, err
	}
	if err := notifySessions(ctx, tx); err != nil {
		return "", false, err
	}
	if err := tx.Commit(); err != nil {
		return "", false, err
	}
	return handle, true, nil
}

func (s *sessionStore) save(ctx context.Context, id string, before, after map[string]any) (bool, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	row, err := scanBrowserRow(tx.QueryRowContext(ctx, `
		SELECT id, expires_at, absolute_expires_at, payload::text,
		       operator_issuer, operator_subject, operator_username,
		       access_token, access_token_expires_at, refresh_token
		FROM operator_browser_session WHERE id = $1 FOR UPDATE`, id))
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if !row.expiresAt.After(s.now().UTC()) {
		if err := deleteAndNotify(ctx, tx, id); err != nil {
			return false, err
		}
		if err := tx.Commit(); err != nil {
			return false, err
		}
		return false, nil
	}
	merged := mergePayload(row.payload, before, after)
	if row.login == nil && len(merged) == 0 {
		if err := deleteAndNotify(ctx, tx, id); err != nil {
			return false, err
		}
		if err := tx.Commit(); err != nil {
			return false, err
		}
		return false, nil
	}
	data, err := json.Marshal(merged)
	if err != nil {
		return false, err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE operator_browser_session SET payload = $2::jsonb WHERE id = $1`, id, string(data)); err != nil {
		return false, err
	}
	if err := tx.Commit(); err != nil {
		return false, err
	}
	return true, nil
}

func mergePayload(current, before, after map[string]any) map[string]any {
	merged := make(map[string]any, len(current)+len(after))
	for key, value := range current {
		_, inBefore := before[key]
		_, inAfter := after[key]
		if !inAfter && inBefore {
			continue
		}
		merged[key] = value
	}
	for key, value := range after {
		old, existed := before[key]
		if !existed || !jsonEqual(old, value) {
			merged[key] = value
		}
	}
	return merged
}

func jsonEqual(a, b any) bool {
	left, errA := json.Marshal(a)
	right, errB := json.Marshal(b)
	return errA == nil && errB == nil && string(left) == string(right)
}

func (s *sessionStore) end(ctx context.Context, id string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := deleteAndNotify(ctx, tx, id); err != nil {
		return err
	}
	return tx.Commit()
}

func deleteAndNotify(ctx context.Context, tx *sql.Tx, id string) error {
	result, err := tx.ExecContext(ctx, `DELETE FROM operator_browser_session WHERE id = $1`, id)
	if err != nil {
		return err
	}
	count, err := result.RowsAffected()
	if err != nil || count == 0 {
		return err
	}
	return notifySessions(ctx, tx)
}

func notifySessions(ctx context.Context, tx *sql.Tx) error {
	_, err := tx.ExecContext(ctx, `SELECT pg_notify($1, '')`, sessionNotifyChannel)
	return err
}

func (s *sessionStore) untilEnded(ctx context.Context, id string, changes ChangeSource) error {
	var signal <-chan struct{}
	var unsubscribe func()
	if changes != nil {
		var err error
		signal, unsubscribe, err = changes.Subscribe(sessionNotifyChannel)
		if err != nil {
			return err
		}
		defer unsubscribe()
	}
	for {
		var expiry time.Time
		err := s.db.QueryRowContext(ctx, `SELECT expires_at FROM operator_browser_session WHERE id = $1`, id).Scan(&expiry)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		remaining := expiry.Sub(s.now().UTC())
		if remaining <= 0 {
			return nil
		}
		timer := time.NewTimer(remaining)
		if signal == nil {
			select {
			case <-ctx.Done():
				timer.Stop()
				return ctx.Err()
			case <-timer.C:
				return nil
			}
		}
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-signal:
			if !timer.Stop() {
				<-timer.C
			}
		case <-timer.C:
			return nil
		}
	}
}

type ChangeSource interface {
	Subscribe(channels ...string) (<-chan struct{}, func(), error)
}

type SessionRow struct {
	store *sessionStore
	id    string
}

// ID returns the database key used by UntilEnded. It is a one-way digest of the browser handle.
func (r *SessionRow) ID() string { return r.id }

// Login returns the current login associated with this browser session after an expiry-aware read.
func (r *SessionRow) Login(ctx context.Context) (*OperatorSession, error) {
	row, err := r.store.read(ctx, r.id)
	if err != nil || row == nil || row.login == nil {
		return nil, err
	}
	return row.login, nil
}

// WithLockedLogin serializes refresh-token use and replacement against logout for this session.
// The callback runs while its row transaction is held, so it must not recursively use this row.
func (r *SessionRow) WithLockedLogin(ctx context.Context, update func(*OperatorSession) (*OperatorSession, error)) error {
	tx, err := r.store.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	row, err := scanBrowserRow(tx.QueryRowContext(ctx, `
		SELECT id, expires_at, absolute_expires_at, payload::text,
		       operator_issuer, operator_subject, operator_username,
		       access_token, access_token_expires_at, refresh_token
		FROM operator_browser_session WHERE id = $1 FOR UPDATE`, r.id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	if !row.expiresAt.After(r.store.now().UTC()) {
		if err := deleteAndNotify(ctx, tx, r.id); err != nil {
			return err
		}
		return tx.Commit()
	}
	if row.login == nil {
		return tx.Commit()
	}
	updated, err := update(row.login)
	if err != nil {
		return err
	}
	if updated == nil {
		if err := deleteAndNotify(ctx, tx, r.id); err != nil {
			return err
		}
		return tx.Commit()
	}
	var access, expiry, refresh any
	if updated.Tokens != nil {
		access, expiry = updated.Tokens.AccessToken, updated.Tokens.ExpiresAt
		if updated.Tokens.RefreshToken != "" {
			refresh = updated.Tokens.RefreshToken
		}
	}
	_, err = tx.ExecContext(ctx, `UPDATE operator_browser_session SET operator_issuer=$2, operator_subject=$3,
		operator_username=$4, access_token=$5, access_token_expires_at=$6, refresh_token=$7 WHERE id=$1`,
		r.id, updated.Issuer, updated.Subject, updated.Username, access, expiry, refresh)
	if err != nil {
		return err
	}
	return tx.Commit()
}
