package session

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

// ID is the one-way database key for a signed browser-session handle.
type ID string

// Attempt is the app-owned state for one authorization-code flow, persisted with the session.
type Attempt struct {
	Nonce        string    `json:"nonce"`
	CodeVerifier string    `json:"code_verifier"`
	ExpiresAt    time.Time `json:"expires_at"`
}

// Tokens are the provider tokens persisted for a logged-in browser session.
type Tokens struct {
	AccessToken  string
	ExpiresAt    time.Time
	RefreshToken string
}

// Login is the authenticated operator and its provider tokens persisted in a browser session.
type Login struct {
	Issuer   string
	Subject  string
	Username string
	Tokens   *Tokens
}

// Record is the persisted browser-session snapshot returned by an expiry-aware read.
type Record struct {
	ID                ID
	ExpiresAt         time.Time
	AbsoluteExpiresAt time.Time
	PendingLogins     map[string]Attempt
	Login             *Login
}

// Store owns browser-session rows and their SQL transactions.
type Store struct {
	db   *sql.DB
	idle time.Duration
	step time.Duration
	now  func() time.Time
}

// NewStore creates a store with the configured idle lifetime and activity update step.
func NewStore(db *sql.DB, idle, step time.Duration) *Store {
	return &Store{db: db, idle: idle, step: step, now: time.Now}
}

// IDForHandle derives the database key from a verified opaque browser-session handle.
func IDForHandle(handle string) ID {
	sum := sha256.Sum256([]byte(handle))
	return ID(hex.EncodeToString(sum[:]))
}

func newHandle() (string, error) {
	bytes := make([]byte, 32)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(bytes), nil
}

// Read returns the current row and advances its idle deadline when the activity step is due.
func (s *Store) Read(ctx context.Context, id ID) (*Record, error) {
	row, err := scanRecord(s.db.QueryRowContext(ctx, `
		SELECT id, expires_at, absolute_expires_at, payload::text,
		       operator_issuer, operator_subject, operator_username,
		       access_token, access_token_expires_at, refresh_token
		FROM operator_browser_session WHERE id = $1`, string(id)))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	now := s.now().UTC()
	if !row.ExpiresAt.After(now) {
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
			DELETE FROM operator_browser_session WHERE id IN (SELECT id FROM candidate) RETURNING id`, string(id), now).Scan(&deleted)
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
	if deadline.After(row.AbsoluteExpiresAt) {
		deadline = row.AbsoluteExpiresAt
	}
	step := s.step
	if half := s.idle / 2; half < step {
		step = half
	}
	if deadline.Sub(row.ExpiresAt) > step {
		var result sql.Result
		result, err = s.db.ExecContext(ctx, `
			WITH candidate AS (
				SELECT id FROM operator_browser_session WHERE id = $1 AND expires_at < $2
				FOR UPDATE SKIP LOCKED
			)
			UPDATE operator_browser_session SET expires_at = $2
			WHERE id IN (SELECT id FROM candidate)`, string(id), deadline)
		if err != nil {
			return nil, err
		}
		if count, countErr := result.RowsAffected(); countErr == nil && count > 0 {
			row.ExpiresAt = deadline
		}
	}
	return row, nil
}

func scanRecord(scanner interface{ Scan(...any) error }) (*Record, error) {
	var row Record
	var id string
	var payload string
	var issuer, subject, username, accessToken, refreshToken sql.NullString
	var tokenExpiry sql.NullTime
	if err := scanner.Scan(&id, &row.ExpiresAt, &row.AbsoluteExpiresAt, &payload,
		&issuer, &subject, &username, &accessToken, &tokenExpiry, &refreshToken); err != nil {
		return nil, err
	}
	row.ID = ID(id)
	if err := json.Unmarshal([]byte(payload), &row.PendingLogins); err != nil {
		return nil, fmt.Errorf("decode browser session payload: %w", err)
	}
	if row.PendingLogins == nil {
		row.PendingLogins = map[string]Attempt{}
	}
	if issuer.Valid {
		row.Login = &Login{Issuer: issuer.String, Subject: subject.String, Username: username.String}
		if accessToken.Valid {
			row.Login.Tokens = &Tokens{AccessToken: accessToken.String, ExpiresAt: tokenExpiry.Time}
			if refreshToken.Valid {
				row.Login.Tokens.RefreshToken = refreshToken.String
			}
		}
	}
	return &row, nil
}

// Create inserts a pending browser session and returns its opaque cookie handle.
func (s *Store) Create(ctx context.Context, payload map[string]Attempt, absolute time.Time) (string, error) {
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
		string(IDForHandle(handle)), deadline, absolute, string(data))
	if err != nil {
		return "", err
	}
	return handle, nil
}

// CompleteLogin atomically consumes a pending row and inserts its authenticated replacement.
func (s *Store) CompleteLogin(ctx context.Context, pendingID ID, payload map[string]Attempt, login Login, absolute time.Time) (string, bool, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return "", false, err
	}
	defer tx.Rollback()
	if pendingID != "" {
		result, err := tx.ExecContext(ctx, `DELETE FROM operator_browser_session WHERE id = $1`, string(pendingID))
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
		VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10)`, string(IDForHandle(handle)), deadline, absolute,
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

// SavePending merges request-local changes into the current row without resurrecting deleted rows.
func (s *Store) SavePending(ctx context.Context, id ID, before, after map[string]Attempt) (bool, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return false, err
	}
	defer tx.Rollback()
	row, err := scanRecord(tx.QueryRowContext(ctx, `
		SELECT id, expires_at, absolute_expires_at, payload::text,
		       operator_issuer, operator_subject, operator_username,
		       access_token, access_token_expires_at, refresh_token
		FROM operator_browser_session WHERE id = $1 FOR UPDATE`, string(id)))
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if !row.ExpiresAt.After(s.now().UTC()) {
		if err := deleteAndNotify(ctx, tx, string(id)); err != nil {
			return false, err
		}
		if err := tx.Commit(); err != nil {
			return false, err
		}
		return false, nil
	}
	merged := mergeLoginAttempts(row.PendingLogins, before, after)
	if row.Login == nil && len(merged) == 0 {
		if err := deleteAndNotify(ctx, tx, string(id)); err != nil {
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
	if _, err := tx.ExecContext(ctx, `UPDATE operator_browser_session SET payload = $2::jsonb WHERE id = $1`, string(id), string(data)); err != nil {
		return false, err
	}
	if err := tx.Commit(); err != nil {
		return false, err
	}
	return true, nil
}

func mergeLoginAttempts(current, before, after map[string]Attempt) map[string]Attempt {
	merged := make(map[string]Attempt, len(current)+len(after))
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
		if !existed || old != value {
			merged[key] = value
		}
	}
	return merged
}

// Terminate deletes the row and notifies watchers in the same transaction.
func (s *Store) Terminate(ctx context.Context, id ID) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := deleteAndNotify(ctx, tx, string(id)); err != nil {
		return err
	}
	return tx.Commit()
}

func deleteAndNotify(ctx context.Context, tx *sql.Tx, id string) error {
	result, err := tx.ExecContext(ctx, `DELETE FROM operator_browser_session WHERE id = $1`, string(id))
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
	_, err := tx.ExecContext(ctx, `SELECT pg_notify($1, '')`, SessionUpdatesChannel)
	return err
}

// UntilTerminated returns when a row is deleted or expires, observing database notifications.
func (s *Store) UntilTerminated(ctx context.Context, id ID, changes ChangeSource) error {
	var signal <-chan struct{}
	var unsubscribe func()
	if changes != nil {
		var err error
		signal, unsubscribe, err = changes.Subscribe(SessionUpdatesChannel)
		if err != nil {
			return err
		}
		defer unsubscribe()
	}
	for {
		var expiry time.Time
		err := s.db.QueryRowContext(ctx, `SELECT expires_at FROM operator_browser_session WHERE id = $1`, string(id)).Scan(&expiry)
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

// RefreshLogin serializes refresh-token use with termination for this row. The update callback
// runs while the row transaction is held; it must not recursively access this row. Returning nil
// deletes and notifies instead of replacing the login. A missing or expired row never calls update.
func (s *Store) RefreshLogin(ctx context.Context, id ID, update func(*Login) (*Login, error)) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	row, err := scanRecord(tx.QueryRowContext(ctx, `
		SELECT id, expires_at, absolute_expires_at, payload::text,
		       operator_issuer, operator_subject, operator_username,
		       access_token, access_token_expires_at, refresh_token
		FROM operator_browser_session WHERE id = $1 FOR UPDATE`, string(id)))
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	if !row.ExpiresAt.After(s.now().UTC()) {
		if err := deleteAndNotify(ctx, tx, string(id)); err != nil {
			return err
		}
		return tx.Commit()
	}
	if row.Login == nil {
		return tx.Commit()
	}
	updated, err := update(row.Login)
	if err != nil {
		return err
	}
	if updated == nil {
		if err := deleteAndNotify(ctx, tx, string(id)); err != nil {
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
		string(id), updated.Issuer, updated.Subject, updated.Username, access, expiry, refresh)
	if err != nil {
		return err
	}
	return tx.Commit()
}
