// Package reads serves the History Service's read RPCs over the fold tables and the raw history: Session
// summaries, Thread entity windows, and the evidence behind an entity. Each method takes an RPC's request and
// returns its response; the gRPC servicer only admits the caller and maps the errors below to status codes.
package reads

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/google/uuid"
)

var (
	// ErrScopeReset means the caller's projection epoch is no longer the Session's current scope.
	ErrScopeReset = errors.New("thread projection epoch was reset")
	// ErrEvidenceNotFound means the Session has no fold, or its fold has no such entity or observation.
	ErrEvidenceNotFound = errors.New("entity evidence not found")
	// ErrInvalidStoredEntity means a stored entity row is outside the published view contract: a fault of the
	// fold's writer, not of the request.
	ErrInvalidStoredEntity = errors.New("stored thread entity is invalid")
)

// Reader reads a Session's folded state from DB and its retained history from History.
type Reader struct {
	DB                       *sql.DB
	History                  *storage.Store
	NativeFrameReadBatchSize int
	scope                    *storage.ReadScope
}

// WithReadScope returns a reader that applies the same retained owner scope to fold and raw-history queries.
func (r *Reader) WithReadScope(scope storage.ReadScope) *Reader {
	copy := *r
	copy.scope = &scope
	copy.History = r.History.WithReadScope(scope)
	return &copy
}

func (r *Reader) sessionPredicate(alias string, firstArgument int) (string, []any) {
	if r.scope == nil {
		return "TRUE", nil
	}
	return r.scope.Predicate(alias, firstArgument)
}

func invalid(detail string) error { return fmt.Errorf("%w: %s", storage.ErrInvalidArgument, detail) }

func parseSessionID(sessionID string) (uuid.UUID, error) {
	parsed, err := uuid.Parse(sessionID)
	if err != nil {
		return uuid.Nil, invalid("session id")
	}
	return parsed, nil
}

// snapshot runs f on one read-only snapshot, so what it reads agrees: the fold commits a Session's checkpoint
// with the rows it covers.
func snapshot(ctx context.Context, db *sql.DB, f func(*sql.Tx) error) error {
	tx, err := db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		return err
	}
	defer tx.Rollback()
	return f(tx)
}

func (r *Reader) requireSession(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID) error {
	predicate, scopeArgs := r.sessionPredicate("h", 2)
	args := append([]any{sessionID}, scopeArgs...)
	var found bool
	err := tx.QueryRowContext(ctx, "SELECT true FROM session_history h WHERE h.id = $1 AND "+predicate, args...).Scan(&found)
	if errors.Is(err, sql.ErrNoRows) {
		return fmt.Errorf("%w: %s", storage.ErrNotFound, sessionID)
	}
	return err
}
