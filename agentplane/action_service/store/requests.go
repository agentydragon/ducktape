package store

import (
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

type querier interface {
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

type rowScanner interface{ Scan(...any) error }

const requestColumns = `id, idempotency_key, action, arguments, title, description, caller_namespace, caller_name,
	state, version, created_at, updated_at, history_at`

// requestRow is a request with the position in history its first non-pending Event gave it.
type requestRow struct {
	ActionRequest
	HistoryAt *time.Time
}

// jsonValue is a JSONB column's document, nil when the column holds none. A JSON null is none: the stored
// models read it as absent too.
func jsonValue(column []byte) json.RawMessage {
	if column == nil || string(column) == "null" {
		return nil
	}
	return column
}

// jsonParam is a JSONB column's parameter: NULL when there is no document.
func jsonParam(document json.RawMessage) any {
	if document == nil {
		return nil
	}
	return string(document)
}

func utc(t *time.Time) *time.Time {
	if t == nil {
		return nil
	}
	converted := t.UTC()
	return &converted
}

func decodeRecordedActor(data []byte) (*action_servicepb.Principal, error) {
	actor, err := action.DecodeStoredActor(data)
	if errors.Is(err, action.ErrInvalidActor) {
		return nil, invalid("%s", err)
	}
	return actor, err
}

func scanRequest(row rowScanner) (requestRow, error) {
	var r requestRow
	var actionJSON, arguments []byte
	var state string
	if err := row.Scan(&r.ID, &r.IdempotencyKey, &actionJSON, &arguments, &r.Title, &r.Description, &r.Caller.Namespace,
		&r.Caller.Name, &state, &r.Version, &r.CreatedAt, &r.UpdatedAt, &r.HistoryAt); err != nil {
		return requestRow{}, err
	}
	if err := json.Unmarshal(actionJSON, &r.Action); err != nil {
		return requestRow{}, fmt.Errorf("request %s action: %w", r.ID, err)
	}
	r.Arguments = arguments
	var known bool
	r.State, known = action.StateFromStored(state)
	if !known {
		return requestRow{}, fmt.Errorf("request %s has unknown state %q", r.ID, state)
	}
	r.CreatedAt, r.UpdatedAt, r.HistoryAt = r.CreatedAt.UTC(), r.UpdatedAt.UTC(), utc(r.HistoryAt)
	return r, nil
}

// getRequest is the request with the ID, locked for update when lock is set, or ErrNotFound.
func getRequest(ctx context.Context, q querier, id uuid.UUID, lock bool) (requestRow, error) {
	query := `SELECT ` + requestColumns + ` FROM action_request WHERE id = $1`
	if lock {
		query += ` FOR UPDATE`
	}
	row, err := scanRequest(q.QueryRowContext(ctx, query, id))
	if errors.Is(err, sql.ErrNoRows) {
		return requestRow{}, ErrNotFound
	}
	return row, err
}

func (r requestRow) mayBeReadBy(principal ReadPrincipal) bool {
	switch principal := principal.(type) {
	case CallerPrincipal:
		return r.Caller == principal.Account
	case OperatorPrincipal, ServiceReaderPrincipal:
		return true
	default:
		return false
	}
}

// readable is the request with the ID if the reader may see it; another owner's request is not found.
func readable(ctx context.Context, q querier, id uuid.UUID, principal ReadPrincipal) (requestRow, error) {
	row, err := getRequest(ctx, q, id, false)
	if err != nil {
		return requestRow{}, err
	}
	if !row.mayBeReadBy(principal) {
		return requestRow{}, ErrNotFound
	}
	return row, nil
}

// attach reads the Decision and Execution of each request, on the same snapshot as the requests.
func attach(ctx context.Context, q querier, rows []requestRow) ([]ActionRequest, error) {
	if len(rows) == 0 {
		return nil, nil
	}
	ids := make(pq.StringArray, len(rows))
	for i, row := range rows {
		ids[i] = row.ID.String()
	}
	decisions, err := loadDecisions(ctx, q, ids)
	if err != nil {
		return nil, err
	}
	executions, err := loadExecutions(ctx, q, ids)
	if err != nil {
		return nil, err
	}
	requests := make([]ActionRequest, len(rows))
	for i, row := range rows {
		requests[i] = row.ActionRequest
		requests[i].Decision, requests[i].Execution = decisions[row.ID], executions[row.ID]
	}
	return requests, nil
}

func loadDecisions(ctx context.Context, q querier, ids pq.StringArray) (map[uuid.UUID]*Decision, error) {
	rows, err := q.QueryContext(ctx, `
		SELECT request_id, id, verdict, provider, operator_issuer, operator_subject, decision_note, reason_code,
			reason_description, policy_evidence, idempotency_key, decided_at
		FROM action_decision WHERE request_id = ANY($1::uuid[])`, ids)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	decisions := map[uuid.UUID]*Decision{}
	for rows.Next() {
		var requestID uuid.UUID
		var decision Decision
		var verdict string
		var issuer, subject *string
		var evidence []byte
		if err := rows.Scan(&requestID, &decision.ID, &verdict, &decision.Provider, &issuer, &subject,
			&decision.DecisionNote, &decision.ReasonCode, &decision.ReasonDescription, &evidence,
			&decision.IdempotencyKey, &decision.DecidedAt); err != nil {
			return nil, err
		}
		decision.Verdict = Verdict(verdict)
		if !decision.Verdict.valid() {
			return nil, fmt.Errorf("decision %s has unknown verdict %q", decision.ID, verdict)
		}
		if issuer != nil && subject != nil {
			decision.Operator = &OperatorPrincipal{Issuer: *issuer, Subject: *subject}
		}
		if document := jsonValue(evidence); document != nil {
			decision.PolicyEvidence = new(PolicyEvidence)
			if err := json.Unmarshal(document, decision.PolicyEvidence); err != nil {
				return nil, fmt.Errorf("decision %s policy_evidence: %w", decision.ID, err)
			}
		}
		decision.DecidedAt = decision.DecidedAt.UTC()
		decisions[requestID] = &decision
	}
	return decisions, rows.Err()
}

func loadExecutions(ctx context.Context, q querier, ids pq.StringArray) (map[uuid.UUID]*Execution, error) {
	rows, err := q.QueryContext(ctx, `
		SELECT request_id, id, state, result, error, created_at, started_at, completed_at, reconciled_at
		FROM action_execution WHERE request_id = ANY($1::uuid[])`, ids)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	executions := map[uuid.UUID]*Execution{}
	for rows.Next() {
		var requestID uuid.UUID
		var execution Execution
		var state string
		var result, failure []byte
		if err := rows.Scan(&requestID, &execution.ID, &state, &result, &failure, &execution.CreatedAt,
			&execution.StartedAt, &execution.CompletedAt, &execution.ReconciledAt); err != nil {
			return nil, err
		}
		execution.State = ExecutionState(state)
		if !execution.State.valid() {
			return nil, fmt.Errorf("execution %s has unknown state %q", execution.ID, state)
		}
		execution.Result, execution.Error = jsonValue(result), jsonValue(failure)
		execution.CreatedAt = execution.CreatedAt.UTC()
		execution.StartedAt, execution.CompletedAt, execution.ReconciledAt =
			utc(execution.StartedAt), utc(execution.CompletedAt), utc(execution.ReconciledAt)
		executions[requestID] = &execution
	}
	return executions, rows.Err()
}

func storedState(state action_servicepb.ActionState) string {
	text, known := action.StateStored(state)
	if !known {
		panic(fmt.Sprintf("unsupported Action state %v", state))
	}
	return text
}

// advance moves the request to state in the caller's transaction as one transition: a version, an Event and the
// request's first place in history.
func advance(ctx context.Context, tx *sql.Tx, row *requestRow, state action_servicepb.ActionState, at time.Time, actor Principal) error {
	row.State, row.UpdatedAt = state, at
	row.Version++
	if state != action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING && row.HistoryAt == nil {
		row.HistoryAt = &at
	}
	if _, err := tx.ExecContext(ctx,
		`UPDATE action_request SET state = $2, version = $3, updated_at = $4, history_at = $5 WHERE id = $1`,
		row.ID, storedState(state), row.Version, at, row.HistoryAt); err != nil {
		return err
	}
	return appendEvent(ctx, tx, *row, actor)
}

// appendEvent records the request's current version and state, and wakes whoever follows the request.
func appendEvent(ctx context.Context, tx *sql.Tx, row requestRow, actor Principal) error {
	var document any
	if actor != nil {
		recorded, err := recordedActor(actor)
		if err != nil {
			return err
		}
		encoded, err := action.EncodeStoredActor(recorded)
		if err != nil {
			return err
		}
		document = string(encoded)
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO action_event (request_id, sequence, state, at, actor) VALUES ($1, $2, $3, $4, $5::jsonb)`,
		row.ID, row.Version, storedState(row.State), row.UpdatedAt, document); err != nil {
		return err
	}
	return notify(ctx, tx, UpdatesChannel, row.ID.String())
}

func recordedActor(principal Principal) (*action_servicepb.Principal, error) {
	switch principal := principal.(type) {
	case CallerPrincipal:
		return &action_servicepb.Principal{Principal: &action_servicepb.Principal_Caller{Caller: principal.Account.Proto()}}, nil
	case OperatorPrincipal:
		return &action_servicepb.Principal{Principal: &action_servicepb.Principal_Operator{Operator: &action_servicepb.Operator{Issuer: principal.Issuer, Subject: principal.Subject}}}, nil
	default:
		return nil, fmt.Errorf("principal %T cannot author an Action event", principal)
	}
}

// Submit stores an admitted request for the caller. The caller's idempotency key is spent whatever the envelope
// says: a second submission under it is a ConflictIdempotencyKeyUsed, and the request is read back by key.
func (s *Store) Submit(ctx context.Context, caller CallerPrincipal, input ActionRequestInput, admission Admission) (ActionRequest, error) {
	if err := input.Validate(); err != nil {
		return ActionRequest{}, err
	}
	if err := validateServiceAccount(caller.Account); err != nil {
		return ActionRequest{}, err
	}
	if admission.Vote != nil {
		if err := admission.Vote.Validate(); err != nil {
			return ActionRequest{}, err
		}
	}
	action, err := json.Marshal(input.Action)
	if err != nil {
		return ActionRequest{}, err
	}
	var view ActionRequest
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		at := now()
		row, err := scanRequest(tx.QueryRowContext(ctx, `
			INSERT INTO action_request (id, idempotency_key, action, arguments, title, description, caller_namespace,
				caller_name, state, version, created_at, updated_at)
			VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8, 'decision_pending', 1, $9, $9)
			ON CONFLICT (caller_namespace, caller_name, idempotency_key) DO NOTHING
			RETURNING `+requestColumns,
			admission.RequestID, input.IdempotencyKey, string(action), string(input.Arguments), input.Title,
			input.Description, caller.Account.Namespace, caller.Account.Name, at))
		if errors.Is(err, sql.ErrNoRows) {
			return &ConflictError{
				Reason:  ConflictIdempotencyKeyUsed,
				Message: "idempotency key already used by this caller; read that request by key instead of resubmitting",
			}
		}
		if err != nil {
			return err
		}
		if err := appendEvent(ctx, tx, row, nil); err != nil {
			return err
		}
		if vote := admission.Vote; vote != nil {
			if _, err := recordDecision(ctx, tx, &row, at, decisionRecord{
				Verdict:           vote.Verdict,
				Provider:          vote.Provider,
				IdempotencyKey:    "auto:" + row.ID.String(),
				ReasonCode:        &vote.ReasonCode,
				ReasonDescription: vote.ReasonDescription,
				PolicyEvidence:    vote.Evidence,
			}); err != nil {
				return err
			}
		}
		views, err := attach(ctx, tx, []requestRow{row})
		if err != nil {
			return err
		}
		view = views[0]
		return nil
	})
	return view, err
}

// ListFilter narrows List. Nil and empty fields do not filter.
type ListFilter struct {
	States         []action_servicepb.ActionState
	IdempotencyKey *string
	// Limit bounds the page; nil returns every match.
	Limit *int
	// AfterID is the last request the reader already has: the page starts after it in newest-first order,
	// wherever it sits relative to the other filters.
	AfterID *uuid.UUID
}

// List returns the requests the reader may see, newest first. A caller sees its own; an operator and a service
// reader see every owner's.
func (s *Store) List(ctx context.Context, principal ReadPrincipal, filter ListFilter) ([]ActionRequest, error) {
	for _, state := range filter.States {
		if _, known := action.StateStored(state); !known {
			return nil, invalid("unknown state %q", state)
		}
	}
	if filter.Limit != nil && *filter.Limit < 1 {
		return nil, invalid("limit must be at least 1")
	}
	var requests []ActionRequest
	err := s.read(ctx, func(tx *sql.Tx) error {
		var q query
		conditions := q.readableBy(principal)
		if len(filter.States) > 0 {
			states := make(pq.StringArray, len(filter.States))
			for i, state := range filter.States {
				states[i] = storedState(state)
			}
			conditions = append(conditions, "state = ANY("+q.arg(states)+")")
		}
		if filter.IdempotencyKey != nil {
			conditions = append(conditions, "idempotency_key = "+q.arg(*filter.IdempotencyKey))
		}
		if filter.AfterID != nil {
			anchor, err := readable(ctx, tx, *filter.AfterID, principal)
			if err != nil {
				return err
			}
			conditions = append(conditions, "(created_at, id) < ("+q.arg(anchor.CreatedAt)+", "+q.arg(anchor.ID)+")")
		}
		statement := `SELECT ` + requestColumns + ` FROM action_request` + where(conditions) + ` ORDER BY created_at DESC, id DESC`
		if filter.Limit != nil {
			statement += ` LIMIT ` + q.arg(*filter.Limit)
		}
		rows, err := scanRequests(ctx, tx, statement, q.args...)
		if err != nil {
			return err
		}
		requests, err = attach(ctx, tx, rows)
		return err
	})
	return requests, err
}

// Versions returns the version of each request the reader may see in one of states, or in any state when states is
// empty. Every visible change to a request advances its version, so a watch compares them to find what changed.
func (s *Store) Versions(ctx context.Context, principal ReadPrincipal, states []action_servicepb.ActionState) (map[uuid.UUID]int64, error) {
	var q query
	conditions := q.readableBy(principal)
	if len(states) > 0 {
		values := make(pq.StringArray, len(states))
		for i, state := range states {
			if _, known := action.StateStored(state); !known {
				return nil, invalid("unknown state %q", state)
			}
			values[i] = storedState(state)
		}
		conditions = append(conditions, "state = ANY("+q.arg(values)+")")
	}
	rows, err := s.db.QueryContext(ctx, `SELECT id, version FROM action_request`+where(conditions), q.args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	versions := map[uuid.UUID]int64{}
	for rows.Next() {
		var id uuid.UUID
		var version int64
		if err := rows.Scan(&id, &version); err != nil {
			return nil, err
		}
		versions[id] = version
	}
	return versions, rows.Err()
}

// Find returns the requests among ids that the reader may see, in no particular order; the others are left out.
func (s *Store) Find(ctx context.Context, principal ReadPrincipal, ids []uuid.UUID) ([]ActionRequest, error) {
	var requests []ActionRequest
	err := s.read(ctx, func(tx *sql.Tx) error {
		var q query
		values := make(pq.StringArray, len(ids))
		for i, id := range ids {
			values[i] = id.String()
		}
		conditions := append(q.readableBy(principal), "id = ANY("+q.arg(values)+"::uuid[])")
		rows, err := scanRequests(ctx, tx, `SELECT `+requestColumns+` FROM action_request`+where(conditions), q.args...)
		if err != nil {
			return err
		}
		requests, err = attach(ctx, tx, rows)
		return err
	})
	return requests, err
}

// query collects the parameters of a statement built from conditions.
type query struct{ args []any }

// arg numbers a value as the next placeholder; calls evaluate left to right, so a condition keeps its order.
func (q *query) arg(value any) string {
	q.args = append(q.args, value)
	return fmt.Sprintf("$%d", len(q.args))
}

// readableBy is the condition mayBeReadBy applies: a caller reads its own requests, an operator and a service
// reader every owner's.
func (q *query) readableBy(principal ReadPrincipal) []string {
	switch principal := principal.(type) {
	case CallerPrincipal:
		return []string{"caller_namespace = " + q.arg(principal.Account.Namespace) + " AND caller_name = " + q.arg(principal.Account.Name)}
	case OperatorPrincipal, ServiceReaderPrincipal:
		return nil
	default:
		return []string{"FALSE"}
	}
}

func where(conditions []string) string {
	if len(conditions) == 0 {
		return ""
	}
	return ` WHERE ` + strings.Join(conditions, ` AND `)
}

func scanRequests(ctx context.Context, q querier, query string, args ...any) ([]requestRow, error) {
	rows, err := q.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var requests []requestRow
	for rows.Next() {
		row, err := scanRequest(rows)
		if err != nil {
			return nil, err
		}
		requests = append(requests, row)
	}
	return requests, rows.Err()
}

// HistoryPage selects a page of History.
type HistoryPage struct {
	Limit int
	// Cursor is the NextCursor of the previous page; nil starts at the newest.
	Cursor *string
	// ExcludeAutoAllowed leaves out requests a DecisionProvider allowed before filling the page.
	ExcludeAutoAllowed bool
}

// History returns the requests the reader may see that left the pending state, newest transition first, and the
// cursor of the next page, which is nil on the last.
func (s *Store) History(ctx context.Context, principal ReadPrincipal, page HistoryPage) ([]ActionRequest, *string, error) {
	if page.Limit < 1 {
		return nil, nil, invalid("limit must be at least 1")
	}
	var after *historyPosition
	if page.Cursor != nil {
		position, err := decodeCursor(*page.Cursor)
		if err != nil {
			return nil, nil, err
		}
		after = &position
	}
	var requests []ActionRequest
	var next *string
	err := s.read(ctx, func(tx *sql.Tx) error {
		var q query
		conditions := append(q.readableBy(principal), "history_at IS NOT NULL")
		if page.ExcludeAutoAllowed {
			conditions = append(conditions, `NOT EXISTS (SELECT 1 FROM action_decision d WHERE d.request_id = action_request.id
				AND d.verdict = 'allow' AND d.operator_issuer IS NULL)`)
		}
		if after != nil {
			conditions = append(conditions, "(history_at, id) < ("+q.arg(after.At)+", "+q.arg(after.ID)+")")
		}
		statement := `SELECT ` + requestColumns + ` FROM action_request` + where(conditions) +
			` ORDER BY history_at DESC, id DESC LIMIT ` + q.arg(page.Limit+1)
		rows, err := scanRequests(ctx, tx, statement, q.args...)
		if err != nil {
			return err
		}
		if len(rows) > page.Limit {
			rows = rows[:page.Limit]
			last := rows[len(rows)-1]
			cursor := encodeCursor(historyPosition{At: *last.HistoryAt, ID: last.ID})
			next = &cursor
		}
		requests, err = attach(ctx, tx, rows)
		return err
	})
	return requests, next, err
}

// Get returns the request with the ID if the reader may see it.
func (s *Store) Get(ctx context.Context, id uuid.UUID, principal ReadPrincipal) (ActionRequest, error) {
	var request ActionRequest
	err := s.read(ctx, func(tx *sql.Tx) error {
		row, err := readable(ctx, tx, id, principal)
		if err != nil {
			return err
		}
		views, err := attach(ctx, tx, []requestRow{row})
		if err != nil {
			return err
		}
		request = views[0]
		return nil
	})
	return request, err
}

// EventsFilter narrows Events.
type EventsFilter struct {
	// AfterSequence is the last sequence the reader already has; polling it back returns nothing new.
	AfterSequence int64
	// Limit bounds the page; nil returns every Event.
	Limit *int
}

// Events returns the request's Events by sequence, if the reader may see the request.
func (s *Store) Events(ctx context.Context, id uuid.UUID, principal ReadPrincipal, filter EventsFilter) ([]Event, error) {
	if filter.AfterSequence < 0 || filter.Limit != nil && *filter.Limit < 1 {
		return nil, invalid("after_sequence must not be negative and limit must be at least 1")
	}
	var events []Event
	err := s.read(ctx, func(tx *sql.Tx) error {
		if _, err := readable(ctx, tx, id, principal); err != nil {
			return err
		}
		query := `SELECT sequence, state, at, actor FROM action_event WHERE request_id = $1 AND sequence > $2
			ORDER BY sequence`
		args := []any{id, filter.AfterSequence}
		if filter.Limit != nil {
			query += ` LIMIT $3`
			args = append(args, *filter.Limit)
		}
		rows, err := tx.QueryContext(ctx, query, args...)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var event Event
			var state string
			var actor []byte
			if err := rows.Scan(&event.Sequence, &state, &event.At, &actor); err != nil {
				return err
			}
			var known bool
			event.State, known = action.StateFromStored(state)
			event.At = event.At.UTC()
			if !known {
				return fmt.Errorf("event %s/%d has unknown state %q", id, event.Sequence, state)
			}
			if document := jsonValue(actor); document != nil {
				var err error
				if event.Actor, err = decodeRecordedActor(document); err != nil {
					return fmt.Errorf("event %s/%d actor: %w", id, event.Sequence, err)
				}
			}
			events = append(events, event)
		}
		return rows.Err()
	})
	return events, err
}

// historyPosition is where a history page ends.
type historyPosition struct {
	At time.Time
	ID uuid.UUID
}

// encodeCursor writes the position as unpadded URL-safe base64 of `["<ISO 8601 time>", "<id>"]`, byte for byte
// what the Python service writes, so a client may page across the two.
func encodeCursor(position historyPosition) string {
	layout := "2006-01-02T15:04:05-07:00"
	if position.At.Nanosecond() != 0 {
		layout = "2006-01-02T15:04:05.000000-07:00"
	}
	document := fmt.Sprintf(`["%s", "%s"]`, position.At.UTC().Format(layout), position.ID)
	return base64.RawURLEncoding.EncodeToString([]byte(document))
}

func decodeCursor(cursor string) (historyPosition, error) {
	if utf8.RuneCountInString(cursor) > 1024 {
		return historyPosition{}, invalid("invalid history cursor")
	}
	document, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(cursor, "="))
	var parts []string
	if err == nil {
		err = json.Unmarshal(document, &parts)
	}
	if err != nil || len(parts) != 2 {
		return historyPosition{}, invalid("invalid history cursor")
	}
	at, atErr := time.Parse(time.RFC3339Nano, parts[0])
	id, idErr := uuid.Parse(parts[1])
	if err := errors.Join(atErr, idErr); err != nil {
		return historyPosition{}, invalid("invalid history cursor")
	}
	return historyPosition{At: at.UTC(), ID: id}, nil
}
