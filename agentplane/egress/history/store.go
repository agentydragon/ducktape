// Package history is the egress proxy's shared decision history: rows every replica appends and every
// replica's admin port reads, a bounded best-effort writer in front of them, and the change stream
// that tells a reader when to read again. Schema changes run only through the migrations package.
package history

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/google/uuid"
)

// decisionsChannel is notified with every committed batch, to every replica listening.
const decisionsChannel = "agentplane_egress_decisions"

const (
	// operationTimeout bounds each database operation, and the wait for a connection to run it.
	operationTimeout = 2 * time.Second
	maxConnections   = 2
	// MaxCapacity is the most decisions a read returns.
	MaxCapacity = 1000
	// appendChunk keeps a statement under PostgreSQL's 65535 parameter limit.
	appendChunk  = 1000
	cleanupBatch = 1000
)

const columns = "event_id, decided_at, producer_id, subject_namespace, subject_name, source_pod_uid, connection_id, " +
	"phase, method, host, port, outcome, reason, binding, policy, rule, substituted, address, " +
	"impersonated_namespace, impersonated_name, subject_identity"

const columnCount = 21

// Open opens the history database with the connection bounds every operation relies on: a small pool
// and a connect timeout, either of which the URL may override.
func Open(databaseURL string) (*sql.DB, error) {
	dsn, err := connectionString(databaseURL, "agentplane-egress-decision-history")
	if err != nil {
		return nil, err
	}
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(maxConnections)
	return db, nil
}

// connectionString adds the connect timeout and application name to a postgres URL unless it sets
// them. The URL is never part of an error: it carries the password.
func connectionString(databaseURL, applicationName string) (string, error) {
	parsed, err := url.Parse(databaseURL)
	if err != nil {
		var urlErr *url.Error
		if errors.As(err, &urlErr) {
			err = urlErr.Err
		}
		return "", fmt.Errorf("invalid database URL: %w", err)
	}
	if parsed.Scheme != "postgres" && parsed.Scheme != "postgresql" {
		return "", fmt.Errorf("database URL must use the postgres or postgresql scheme, not %q", parsed.Scheme)
	}
	query := parsed.Query()
	for key, value := range map[string]string{
		"connect_timeout":  strconv.Itoa(int(operationTimeout.Seconds())),
		"application_name": applicationName,
	} {
		if !query.Has(key) {
			query.Set(key, value)
		}
	}
	parsed.RawQuery = query.Encode()
	return parsed.String(), nil
}

// Store is the decision table. Appends are idempotent on the event ID, so a write whose
// acknowledgement was lost can simply be sent again.
type Store struct {
	db        *sql.DB
	retention time.Duration
	capacity  int
}

// Window is the per-subject visible history bound used by Recent and decision watches.
type Window struct {
	Retention time.Duration
	Capacity  int
}

// NewStore reads and keeps decisions for `retention`, returning at most `capacity` per read.
func NewStore(db *sql.DB, retention time.Duration, capacity int) (*Store, error) {
	if retention <= 0 || capacity < 1 || capacity > MaxCapacity {
		return nil, fmt.Errorf("positive retention and capacity between 1 and %d required: %v, %d", MaxCapacity, retention, capacity)
	}
	return &Store{db: db, retention: retention, capacity: capacity}, nil
}

// Window reports the retention and capacity the store applies to every subject read.
func (s *Store) Window() Window { return Window{Retention: s.retention, Capacity: s.capacity} }

// Append stores the records and notifies listening replicas, in one transaction.
func (s *Store) Append(ctx context.Context, records []decision.Record) error {
	if len(records) == 0 {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, operationTimeout)
	defer cancel()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for chunk := range slices.Chunk(records, appendChunk) {
		query, args := insertStatement(chunk)
		if _, err := tx.ExecContext(ctx, query, args...); err != nil {
			return err
		}
	}
	for _, payload := range notificationPayloads(records) {
		if err := postgres.Notify(ctx, tx, decisionsChannel, payload); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func insertStatement(records []decision.Record) (string, []any) {
	var query strings.Builder
	query.WriteString("INSERT INTO egress_decision (" + columns + ") VALUES ")
	args := make([]any, 0, len(records)*columnCount)
	for i, record := range records {
		if i > 0 {
			query.WriteByte(',')
		}
		query.WriteByte('(')
		for column := range columnCount {
			if column > 0 {
				query.WriteByte(',')
			}
			fmt.Fprintf(&query, "$%d", i*columnCount+column+1)
		}
		query.WriteByte(')')
		namespace, name, identity := subjectColumns(record.Subject)
		var impersonatedNamespace, impersonatedName *string
		if account := record.Impersonated; account != nil {
			impersonatedNamespace, impersonatedName = &account.Namespace, &account.Name
		}
		args = append(args, record.EventID, record.At, record.ProducerID, namespace, name, record.SourcePodUID,
			record.ConnectionID, record.Phase, record.Method, record.Host, record.Port, record.Outcome,
			record.Reason, record.Binding, record.Policy, record.Rule, record.Substituted, record.Address,
			impersonatedNamespace, impersonatedName, identity)
	}
	query.WriteString(" ON CONFLICT (event_id) DO NOTHING")
	return query.String(), args
}

// subjectColumns is a subject as its row stores it: a ServiceAccount's namespace and name, or an
// Identity's name; all three are NULL for a request that never authenticated.
func subjectColumns(subject decision.Subject) (namespace, name, identity *string) {
	switch subject := subject.(type) {
	case nil:
		return nil, nil, nil
	case decision.ServiceAccount:
		return &subject.Namespace, &subject.Name, nil
	case decision.Identity:
		return nil, nil, &subject.Name
	default:
		panic(fmt.Sprintf("unknown decision subject %T", subject))
	}
}

// scanRecord reads one row selected as `columns`.
func scanRecord(rows *sql.Rows) (decision.Record, error) {
	var record decision.Record
	var namespace, name, impersonatedNamespace, impersonatedName, identity sql.NullString
	if err := rows.Scan(&record.EventID, &record.At, &record.ProducerID, &namespace, &name, &record.SourcePodUID,
		&record.ConnectionID, &record.Phase, &record.Method, &record.Host, &record.Port, &record.Outcome,
		&record.Reason, &record.Binding, &record.Policy, &record.Rule, &record.Substituted, &record.Address,
		&impersonatedNamespace, &impersonatedName, &identity); err != nil {
		return decision.Record{}, err
	}
	// The table's constraints admit at most one subject kind per row.
	switch {
	case namespace.Valid && name.Valid:
		record.Subject = decision.ServiceAccount{Namespace: namespace.String, Name: name.String}
	case identity.Valid:
		record.Subject = decision.Identity{Name: identity.String}
	}
	if impersonatedNamespace.Valid && impersonatedName.Valid {
		record.Impersonated = &decision.ServiceAccount{Namespace: impersonatedNamespace.String, Name: impersonatedName.String}
	}
	return record, nil
}

// Recent is one subject's retained decisions, oldest first and at most the store's capacity of the
// latest, or -- for a nil subject -- those of refusals that never authenticated.
func (s *Store) Recent(ctx context.Context, subject decision.Subject) ([]decision.Record, error) {
	ctx, cancel := context.WithTimeout(ctx, operationTimeout)
	defer cancel()
	var where string
	var args []any
	switch subject := subject.(type) {
	case nil:
		where = "subject_namespace IS NULL AND subject_name IS NULL AND subject_identity IS NULL"
	case decision.ServiceAccount:
		where, args = "subject_namespace = $1 AND subject_name = $2", []any{subject.Namespace, subject.Name}
	case decision.Identity:
		where, args = "subject_identity = $1", []any{subject.Name}
	default:
		panic(fmt.Sprintf("unknown decision subject %T", subject))
	}
	args = append(args, time.Now().Add(-s.retention), s.capacity)
	rows, err := s.db.QueryContext(ctx, fmt.Sprintf(
		"SELECT %s FROM egress_decision WHERE %s AND decided_at >= $%d ORDER BY decided_at DESC, event_id DESC LIMIT $%d",
		columns, where, len(args)-1, len(args),
	), args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	records := []decision.Record{}
	for rows.Next() {
		record, err := scanRecord(rows)
		if err != nil {
			return nil, err
		}
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	slices.Reverse(records)
	return records, nil
}

// ByEventIDs reads only the decisions named by committed change notifications. The returned order
// is not meaningful; callers merge records by event identity and decision time.
func (s *Store) ByEventIDs(ctx context.Context, eventIDs []uuid.UUID) ([]decision.Record, error) {
	if len(eventIDs) == 0 {
		return []decision.Record{}, nil
	}
	ctx, cancel := context.WithTimeout(ctx, operationTimeout)
	defer cancel()
	args := make([]any, len(eventIDs))
	placeholders := make([]string, len(eventIDs))
	for i, eventID := range eventIDs {
		args[i] = eventID
		placeholders[i] = fmt.Sprintf("$%d", i+1)
	}
	rows, err := s.db.QueryContext(ctx, fmt.Sprintf(
		"SELECT %s FROM egress_decision WHERE event_id IN (%s)", columns, strings.Join(placeholders, ","),
	), args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	records := make([]decision.Record, 0, len(eventIDs))
	for rows.Next() {
		record, err := scanRecord(rows)
		if err != nil {
			return nil, err
		}
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return records, nil
}

// Readable checks that the history table can be queried without loading a subject window. Watch
// heartbeats use it to retain their history-read health contract at a bounded, shared cost.
func (s *Store) Readable(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, operationTimeout)
	defer cancel()
	var exists bool
	return s.db.QueryRowContext(
		ctx,
		"SELECT EXISTS (SELECT 1 FROM egress_decision WHERE decided_at >= $1 LIMIT 1)",
		time.Now().Add(-s.retention),
	).Scan(&exists)
}

// Cleanup deletes one batch of expired decisions. Fixed-size, SKIP LOCKED batches keep concurrent
// replicas' cleanup bounded and non-exclusive.
func (s *Store) Cleanup(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, operationTimeout)
	defer cancel()
	_, err := s.db.ExecContext(ctx, `
DELETE FROM egress_decision WHERE event_id IN (
    SELECT event_id FROM egress_decision WHERE decided_at < $1
    ORDER BY decided_at, event_id LIMIT $2 FOR UPDATE SKIP LOCKED)`,
		time.Now().Add(-s.retention), cleanupBatch)
	return err
}
