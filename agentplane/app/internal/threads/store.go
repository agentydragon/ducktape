// Package threads stores the app-owned metadata for each History Session.
package threads

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
)

const (
	UpdatesChannel = "agentplane_thread_updates"
	MaxPage        = 200
)

// ErrNotFound means an app-owned thread row does not exist.
var ErrNotFound = errors.New("thread not found")

// Thread is the app-owned metadata for one History Session. The History Service remains the authority
// for its Sandbox identity and folded activity.
type Thread struct {
	ID        uuid.UUID
	CreatedAt time.Time
	Name      *string
	Archived  bool
}

// PageCursor is the final row of a metadata page, in newest-first listing order.
type PageCursor struct {
	CreatedAt time.Time
	ID        uuid.UUID
}

// Page contains app-owned metadata and a cursor when another page may exist. Callers join History
// summaries separately and can apply History-owned filters without coupling this store to that service.
type Page struct {
	Threads []Thread
	Next    *PageCursor
}

// Store persists app-owned thread metadata in PostgreSQL.
type Store struct {
	db  *sql.DB
	now func() time.Time
}

// NewStore creates a thread metadata store.
func NewStore(db *sql.DB) *Store { return &Store{db: db, now: time.Now} }

// OpenThread creates the row before a newly opened History Session can receive a user message.
// Reopening the same ID leaves the existing row unchanged.
func (s *Store) OpenThread(ctx context.Context, id uuid.UUID) error {
	return withTx(ctx, s.db, func(tx *sql.Tx) error {
		var inserted uuid.UUID
		err := tx.QueryRowContext(ctx, `
			INSERT INTO thread (id, created_at)
			VALUES ($1, $2)
			ON CONFLICT (id) DO NOTHING
			RETURNING id
		`, id, s.now().UTC()).Scan(&inserted)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		return notify(ctx, tx)
	})
}

// GetThread returns the metadata row, or nil when the History Session has not been opened by the app.
func (s *Store) GetThread(ctx context.Context, id uuid.UUID) (*Thread, error) {
	thread, err := scanThread(s.db.QueryRowContext(ctx, `
		SELECT id, created_at, name, archived
		  FROM thread WHERE id = $1
	`, id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &thread, nil
}

// ListPage returns newest-first app metadata. The cursor and archive filter are applied in PostgreSQL;
// History-backed sandbox and activity fields are joined by the caller.
func (s *Store) ListPage(ctx context.Context, limit int, after *PageCursor, includeArchived bool) (Page, error) {
	if limit < 1 || limit > MaxPage {
		return Page{}, fmt.Errorf("thread page size must be between 1 and %d: %d", MaxPage, limit)
	}
	query := `
		SELECT id, created_at, name, archived
		  FROM thread
		 WHERE ($1 OR archived = false)
		 ORDER BY created_at DESC, id DESC
		 LIMIT $2
	`
	args := []any{includeArchived, limit + 1}
	if after != nil {
		query = `
			SELECT id, created_at, name, archived
			  FROM thread
			 WHERE ($1 OR archived = false)
			   AND (created_at, id) < ($2, $3)
			 ORDER BY created_at DESC, id DESC
			 LIMIT $4
		`
		args = []any{includeArchived, after.CreatedAt, after.ID, limit + 1}
	}
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return Page{}, err
	}
	defer rows.Close()
	page := Page{Threads: make([]Thread, 0, limit)}
	for rows.Next() {
		thread, err := scanThread(rows)
		if err != nil {
			return Page{}, err
		}
		if len(page.Threads) == limit {
			last := page.Threads[len(page.Threads)-1]
			page.Next = &PageCursor{CreatedAt: last.CreatedAt, ID: last.ID}
			break
		}
		page.Threads = append(page.Threads, thread)
	}
	if err := rows.Err(); err != nil {
		return Page{}, err
	}
	return page, nil
}

// Rename sets or clears an operator-controlled title.
func (s *Store) Rename(ctx context.Context, id uuid.UUID, name *string) (Thread, error) {
	var thread Thread
	err := withTx(ctx, s.db, func(tx *sql.Tx) error {
		var err error
		thread, err = scanThread(tx.QueryRowContext(ctx, `
			UPDATE thread
			   SET name = $2
			 WHERE id = $1
			RETURNING id, created_at, name, archived
		`, id, name))
		if errors.Is(err, sql.ErrNoRows) {
			return fmt.Errorf("%w: %s", ErrNotFound, id)
		}
		if err != nil {
			return err
		}
		return notify(ctx, tx)
	})
	return thread, err
}

// Archive hides a thread from default listings without touching History data.
func (s *Store) Archive(ctx context.Context, id uuid.UUID) (Thread, error) {
	return s.setArchived(ctx, id, true)
}

// Unarchive makes a previously hidden thread visible in default listings.
func (s *Store) Unarchive(ctx context.Context, id uuid.UUID) (Thread, error) {
	return s.setArchived(ctx, id, false)
}

func (s *Store) setArchived(ctx context.Context, id uuid.UUID, archived bool) (Thread, error) {
	var thread Thread
	err := withTx(ctx, s.db, func(tx *sql.Tx) error {
		var err error
		thread, err = scanThread(tx.QueryRowContext(ctx, `
			UPDATE thread SET archived = $2 WHERE id = $1
			RETURNING id, created_at, name, archived
		`, id, archived))
		if errors.Is(err, sql.ErrNoRows) {
			return fmt.Errorf("%w: %s", ErrNotFound, id)
		}
		if err != nil {
			return err
		}
		return notify(ctx, tx)
	})
	return thread, err
}

// ArchivedCount returns how many app-owned thread rows are hidden from default listings.
func (s *Store) ArchivedCount(ctx context.Context) (int, error) {
	var count int
	err := s.db.QueryRowContext(ctx, `SELECT count(*) FROM thread WHERE archived`).Scan(&count)
	return count, err
}

func withTx(ctx context.Context, db *sql.DB, f func(*sql.Tx) error) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := f(tx); err != nil {
		return err
	}
	return tx.Commit()
}

func notify(ctx context.Context, tx *sql.Tx) error {
	_, err := tx.ExecContext(ctx, `SELECT pg_notify($1, '')`, UpdatesChannel)
	return err
}

func scanThread(scanner interface{ Scan(...any) error }) (Thread, error) {
	var thread Thread
	var name sql.NullString
	err := scanner.Scan(
		&thread.ID,
		&thread.CreatedAt,
		&name,
		&thread.Archived,
	)
	if err != nil {
		return Thread{}, err
	}
	if name.Valid {
		thread.Name = &name.String
	}
	return thread, nil
}
