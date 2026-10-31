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
	"github.com/agentydragon/ducktape/agentplane/postgres/go"
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
	"phase, method, host, port, outcome, reason, binding, policy, rule, substituted, address"

const columnCount = 18

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

// NewStore reads and keeps decisions for `retention`, returning at most `capacity` per read.
func NewStore(db *sql.DB, retention time.Duration, capacity int) (*Store, error) {
	if retention <= 0 || capacity < 1 || capacity > MaxCapacity {
		return nil, fmt.Errorf("positive retention and capacity between 1 and %d required: %v, %d", MaxCapacity, retention, capacity)
	}
	return &Store{db: db, retention: retention, capacity: capacity}, nil
}

// Append stores the records and notifies listening replicas, in one transaction.
func (s *Store) Append(ctx context.Context, records []decision.Record) error {
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
	if err := postgres.Notify(ctx, tx, decisionsChannel, ""); err != nil {
		return err
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
		var namespace, name *string
		if record.Subject != nil {
			namespace, name = &record.Subject.Namespace, &record.Subject.Name
		}
		args = append(args, record.EventID, record.At, record.ProducerID, namespace, name, record.SourcePodUID,
			record.ConnectionID, record.Phase, record.Method, record.Host, record.Port, record.Outcome,
			record.Reason, record.Binding, record.Policy, record.Rule, record.Substituted, record.Address)
	}
	query.WriteString(" ON CONFLICT (event_id) DO NOTHING")
	return query.String(), args
}

// Recent is one subject's retained decisions, oldest first and at most the store's capacity of the
// latest, or -- for a nil subject -- those of refusals that never authenticated.
func (s *Store) Recent(ctx context.Context, subject *decision.Subject) ([]decision.Record, error) {
	ctx, cancel := context.WithTimeout(ctx, operationTimeout)
	defer cancel()
	where, args := "subject_namespace IS NULL AND subject_name IS NULL", []any{}
	if subject != nil {
		where, args = "subject_namespace = $1 AND subject_name = $2", []any{subject.Namespace, subject.Name}
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
		var record decision.Record
		var namespace, name sql.NullString
		if err := rows.Scan(&record.EventID, &record.At, &record.ProducerID, &namespace, &name, &record.SourcePodUID,
			&record.ConnectionID, &record.Phase, &record.Method, &record.Host, &record.Port, &record.Outcome,
			&record.Reason, &record.Binding, &record.Policy, &record.Rule, &record.Substituted, &record.Address); err != nil {
			return nil, err
		}
		if namespace.Valid && name.Valid {
			record.Subject = &decision.Subject{Namespace: namespace.String, Name: name.String}
		}
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	slices.Reverse(records)
	return records, nil
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
