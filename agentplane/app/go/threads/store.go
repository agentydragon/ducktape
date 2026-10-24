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
	UpdatesChannel     = "agentplane_thread_updates"
	MaxPage            = 200
	titleClaimDuration = 5 * time.Minute
)

var (
	// ErrNotFound means an app-owned thread row does not exist.
	ErrNotFound = errors.New("thread not found")
	// ErrConcurrentTitleUpdate means automatic title generation lost its optimistic revision.
	ErrConcurrentTitleUpdate = errors.New("concurrent title update")
)

// Thread is the app-owned metadata for one History Session. The History Service remains the authority
// for its Sandbox identity and folded activity.
type Thread struct {
	ID                      uuid.UUID
	CreatedAt               time.Time
	Name                    *string
	Archived                bool
	AutoTitleOnFirstMessage bool
	TitleUserManaged        bool
	TitleStatus             string
	TitleScanCursor         int64
	TitleClaimedAt          *time.Time
	TitleRevision           int
}

// PendingTitle is an unnamed auto-title thread and the last History cursor already scanned.
type PendingTitle struct {
	ID     uuid.UUID
	Cursor int64
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
func (s *Store) OpenThread(ctx context.Context, id uuid.UUID, autoTitle bool) error {
	return withTx(ctx, s.db, func(tx *sql.Tx) error {
		var inserted uuid.UUID
		err := tx.QueryRowContext(ctx, `
			INSERT INTO thread (id, created_at, auto_title_on_first_message)
			VALUES ($1, $2, $3)
			ON CONFLICT (id) DO NOTHING
			RETURNING id
		`, id, s.now().UTC(), autoTitle).Scan(&inserted)
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
		SELECT id, created_at, name, archived, auto_title_on_first_message, title_user_managed,
		       title_status, title_scan_cursor, title_claimed_at, title_revision
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
		SELECT id, created_at, name, archived, auto_title_on_first_message, title_user_managed,
		       title_status, title_scan_cursor, title_claimed_at, title_revision
		  FROM thread
		 WHERE ($1 OR archived = false)
		 ORDER BY created_at DESC, id DESC
		 LIMIT $2
	`
	args := []any{includeArchived, limit + 1}
	if after != nil {
		query = `
			SELECT id, created_at, name, archived, auto_title_on_first_message, title_user_managed,
			       title_status, title_scan_cursor, title_claimed_at, title_revision
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
			   SET name = $2, title_user_managed = true, title_status = 'done',
			       title_claimed_at = NULL, title_revision = title_revision + 1
			 WHERE id = $1
			RETURNING id, created_at, name, archived, auto_title_on_first_message, title_user_managed,
			          title_status, title_scan_cursor, title_claimed_at, title_revision
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
			RETURNING id, created_at, name, archived, auto_title_on_first_message, title_user_managed,
			          title_status, title_scan_cursor, title_claimed_at, title_revision
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

// PendingAutoTitles returns threads still awaiting their first-message scan or whose claim went stale.
func (s *Store) PendingAutoTitles(ctx context.Context) ([]PendingTitle, error) {
	staleBefore := s.now().UTC().Add(-titleClaimDuration)
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, title_scan_cursor
		  FROM thread
		 WHERE auto_title_on_first_message
		   AND NOT title_user_managed
		   AND name IS NULL
		   AND (
		       title_status = 'waiting'
		       OR (title_status = 'generating' AND title_claimed_at < $1)
		   )
		 ORDER BY created_at
	`, staleBefore)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	pending := make([]PendingTitle, 0)
	for rows.Next() {
		var item PendingTitle
		if err := rows.Scan(&item.ID, &item.Cursor); err != nil {
			return nil, err
		}
		pending = append(pending, item)
	}
	return pending, rows.Err()
}

// AdvanceTitleScanCursor advances the scan checkpoint only while the thread is waiting for a claim.
func (s *Store) AdvanceTitleScanCursor(ctx context.Context, id uuid.UUID, cursor int64) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE thread SET title_scan_cursor = $2
		 WHERE id = $1 AND title_status = 'waiting'
	`, id, cursor)
	return err
}

// ClaimAutoTitle atomically claims a thread across replicas, recovering claims older than five minutes.
func (s *Store) ClaimAutoTitle(ctx context.Context, id uuid.UUID, cursor int64) (*int, error) {
	now := s.now().UTC()
	staleBefore := now.Add(-titleClaimDuration)
	var revision int
	err := s.db.QueryRowContext(ctx, `
		UPDATE thread
		   SET title_status = 'generating', title_claimed_at = $3, title_scan_cursor = $2
		 WHERE id = $1
		   AND auto_title_on_first_message
		   AND NOT title_user_managed
		   AND name IS NULL
		   AND (
		       title_status = 'waiting'
		       OR (title_status = 'generating' AND title_claimed_at < $4)
		   )
		RETURNING title_revision
	`, id, cursor, now, staleBefore).Scan(&revision)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &revision, nil
}

// GeneratedTitle applies on-demand generation only if the user-visible title revision still matches.
func (s *Store) GeneratedTitle(ctx context.Context, id uuid.UUID, title string, expectedRevision int) (Thread, error) {
	var thread Thread
	err := withTx(ctx, s.db, func(tx *sql.Tx) error {
		var err error
		thread, err = scanThread(tx.QueryRowContext(ctx, `
			UPDATE thread
			   SET name = $3, title_status = 'done', title_claimed_at = NULL,
			       title_revision = title_revision + 1
			 WHERE id = $1 AND title_revision = $2
			RETURNING id, created_at, name, archived, auto_title_on_first_message, title_user_managed,
			          title_status, title_scan_cursor, title_claimed_at, title_revision
		`, id, expectedRevision, title))
		if errors.Is(err, sql.ErrNoRows) {
			return fmt.Errorf("%w: thread %s was renamed while generating its title", ErrConcurrentTitleUpdate, id)
		}
		if err != nil {
			return err
		}
		return notify(ctx, tx)
	})
	return thread, err
}

// ApplyFirstMessageTitle completes a first-message claim only while it is still owned by this revision.
func (s *Store) ApplyFirstMessageTitle(ctx context.Context, id uuid.UUID, title string, expectedRevision int) (bool, error) {
	updated := false
	err := withTx(ctx, s.db, func(tx *sql.Tx) error {
		var updatedID uuid.UUID
		err := tx.QueryRowContext(ctx, `
			UPDATE thread
			   SET name = $3, title_status = 'done', title_claimed_at = NULL,
			       title_revision = title_revision + 1
			 WHERE id = $1
			   AND title_status = 'generating'
			   AND title_revision = $2
			   AND NOT title_user_managed
			   AND name IS NULL
			RETURNING id
		`, id, expectedRevision, title).Scan(&updatedID)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		updated = true
		return notify(ctx, tx)
	})
	return updated, err
}

// FailAutoTitle records a failed generation only while the same claim is still active.
func (s *Store) FailAutoTitle(ctx context.Context, id uuid.UUID, expectedRevision int) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE thread
		   SET title_status = 'failed', title_claimed_at = NULL
		 WHERE id = $1 AND title_status = 'generating' AND title_revision = $2
	`, id, expectedRevision)
	return err
}

// TitleContext returns the current title revision and title for on-demand generation.
func (s *Store) TitleContext(ctx context.Context, id uuid.UUID) (int, *string, error) {
	var revision int
	var name sql.NullString
	err := s.db.QueryRowContext(ctx, `SELECT title_revision, name FROM thread WHERE id = $1`, id).
		Scan(&revision, &name)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, nil, fmt.Errorf("%w: %s", ErrNotFound, id)
	}
	if err != nil {
		return 0, nil, err
	}
	if !name.Valid {
		return revision, nil, nil
	}
	return revision, &name.String, nil
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
	var claimedAt sql.NullTime
	err := scanner.Scan(
		&thread.ID,
		&thread.CreatedAt,
		&name,
		&thread.Archived,
		&thread.AutoTitleOnFirstMessage,
		&thread.TitleUserManaged,
		&thread.TitleStatus,
		&thread.TitleScanCursor,
		&claimedAt,
		&thread.TitleRevision,
	)
	if err != nil {
		return Thread{}, err
	}
	if name.Valid {
		thread.Name = &name.String
	}
	if claimedAt.Valid {
		thread.TitleClaimedAt = &claimedAt.Time
	}
	return thread, nil
}
