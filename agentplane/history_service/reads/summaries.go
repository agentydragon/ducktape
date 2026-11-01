package reads

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const maxSummaries = 200

// likeEscaper makes text match itself in a LIKE pattern.
var likeEscaper = strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`)

func (r *Reader) GetSessionSummary(ctx context.Context, request *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	sessionID, err := parseSessionID(request.GetSessionId())
	if err != nil {
		return nil, err
	}
	summaries, err := r.summaries(ctx, []uuid.UUID{sessionID})
	if err != nil {
		return nil, err
	}
	if len(summaries) == 0 {
		return nil, fmt.Errorf("%w: %s", storage.ErrNotFound, sessionID)
	}
	return summaries[0], nil
}

// BatchGetSessionSummaries leaves out a Session that has no history.
func (r *Reader) BatchGetSessionSummaries(ctx context.Context, request *historypb.BatchGetSessionSummariesRequest) (*historypb.BatchGetSessionSummariesResponse, error) {
	distinct := map[uuid.UUID]struct{}{}
	for _, id := range request.GetSessionIds() {
		sessionID, err := parseSessionID(id)
		if err != nil {
			return nil, err
		}
		distinct[sessionID] = struct{}{}
	}
	if len(distinct) > maxSummaries {
		return nil, invalid("too many Sessions in one summary read")
	}
	sessionIDs := make([]uuid.UUID, 0, len(distinct))
	for sessionID := range distinct {
		sessionIDs = append(sessionIDs, sessionID)
	}
	summaries, err := r.summaries(ctx, sessionIDs)
	return &historypb.BatchGetSessionSummariesResponse{Summaries: summaries}, err
}

// FindSessions is every Session written by a Sandbox with the exact name, or whose name contains the text.
func (r *Reader) FindSessions(ctx context.Context, request *historypb.FindSessionsRequest) (*historypb.FindSessionsResponse, error) {
	var match, name string
	var argument any
	switch sandbox := request.GetSandbox().(type) {
	case *historypb.FindSessionsRequest_SandboxName:
		match, name, argument = "h.sandbox_name = $1", sandbox.SandboxName, sandbox.SandboxName
	case *historypb.FindSessionsRequest_SandboxNameContains:
		match, name = `h.sandbox_name ILIKE '%' || $1 || '%'`, likeEscaper.Replace(sandbox.SandboxNameContains)
		argument = name
	case *historypb.FindSessionsRequest_SandboxUid:
		match, name, argument = "h.sandbox_uid = $1", sandbox.SandboxUid, sandbox.SandboxUid
	}
	if name == "" {
		return nil, invalid("a non-empty Sandbox name or UID match is required")
	}
	predicate, scopeArgs := r.sessionPredicate("h", 2)
	args := append([]any{argument}, scopeArgs...)
	rows, err := r.DB.QueryContext(ctx, "SELECT h.id, h.sandbox_namespace, h.sandbox_name, h.sandbox_uid FROM session_history h WHERE ("+match+") AND "+predicate+" ORDER BY h.id", args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	response := &historypb.FindSessionsResponse{}
	for rows.Next() {
		var sessionID uuid.UUID
		sandbox := &historypb.SandboxRef{}
		var uid sql.NullString
		if err := rows.Scan(&sessionID, &sandbox.Namespace, &sandbox.Name, &uid); err != nil {
			return nil, err
		}
		if uid.Valid {
			sandbox.Uid = &uid.String
		}
		response.Sessions = append(response.Sessions, &historypb.SessionSandbox{SessionId: sessionID.String(), Sandbox: sandbox})
	}
	return response, rows.Err()
}

// summaries joins outer: a Session the fold has not discovered yet has neither a summary nor a checkpoint row.
func (r *Reader) summaries(ctx context.Context, sessionIDs []uuid.UUID) ([]*historypb.SessionSummary, error) {
	ids := make([]string, len(sessionIDs))
	for index, sessionID := range sessionIDs {
		ids[index] = sessionID.String()
	}
	predicate, scopeArgs := r.sessionPredicate("h", 2)
	args := append([]any{pq.Array(ids)}, scopeArgs...)
	rows, err := r.DB.QueryContext(ctx, `SELECT h.id, h.last_cursor, h.sandbox_namespace, h.sandbox_name, h.sandbox_uid,
			c.projection_epoch, c.through_cursor, s.first_event_at, s.last_event_at, s.last_model_activity_at,
			s.idle_since, s.last_turn_status, s.attached, s."end"
		FROM session_history h
		LEFT JOIN thread_history_summary s ON s.thread_id = h.id
		LEFT JOIN thread_checkpoint c ON c.thread_id = h.id
		WHERE h.id = ANY($1::uuid[]) AND `+predicate+` ORDER BY h.id`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var summaries []*historypb.SessionSummary
	for rows.Next() {
		var (
			sessionID                                           uuid.UUID
			committed                                           uint64
			namespace, name, epoch                              sql.Null[string]
			sandboxUID                                          sql.Null[string]
			folded                                              sql.Null[uint64]
			firstEvent, lastEvent, lastModelActivity, idleSince sql.Null[time.Time]
			turnStatus                                          sql.Null[eventpb.TurnStatus]
			attached, end                                       []byte
		)
		if err := rows.Scan(&sessionID, &committed, &namespace, &name, &sandboxUID, &epoch, &folded, &firstEvent, &lastEvent,
			&lastModelActivity, &idleSince, &turnStatus, &attached, &end); err != nil {
			return nil, err
		}
		summary := &historypb.SessionSummary{
			SessionId: sessionID.String(), CommittedCursor: committed, FoldedThrough: folded.V,
			FirstEventAt: timestamp(firstEvent), LastEventAt: timestamp(lastEvent),
			LastModelActivityAt: timestamp(lastModelActivity), IdleSince: timestamp(idleSince),
		}
		if namespace.Valid && name.Valid {
			summary.Sandbox = &historypb.SandboxRef{Namespace: namespace.V, Name: name.V}
			if sandboxUID.Valid {
				summary.Sandbox.Uid = &sandboxUID.V
			}
		}
		if epoch.Valid {
			summary.ProjectionEpoch = &epoch.V
		}
		if turnStatus.Valid {
			summary.LastTurnStatus = &turnStatus.V
		}
		if attached != nil {
			summary.Attached = &runnerpb.Attached{}
			if err := protojson.Unmarshal(attached, summary.Attached); err != nil {
				return nil, fmt.Errorf("attachment of Session %s: %w", sessionID, err)
			}
			if summary.FeedStatus, summary.FeedError, err = feedStatus(end); err != nil {
				return nil, fmt.Errorf("end of Session %s: %w", sessionID, err)
			}
		}
		summaries = append(summaries, summary)
	}
	return summaries, rows.Err()
}

func timestamp(at sql.Null[time.Time]) *timestamppb.Timestamp {
	if !at.Valid {
		return nil
	}
	return timestamppb.New(at.V)
}

// feedStatus reads the stored end of a feed: NULL while it runs, `{}` when it ended cleanly, or the failure.
func feedStatus(end []byte) (historypb.FeedStatus, *string, error) {
	if end == nil {
		return historypb.FeedStatus_FEED_STATUS_ACTIVE, nil, nil
	}
	var failure map[string]string
	if err := json.Unmarshal(end, &failure); err != nil {
		return 0, nil, err
	}
	if len(failure) == 0 {
		return historypb.FeedStatus_FEED_STATUS_ENDED, nil, nil
	}
	message, found := failure["message"]
	if !found {
		return 0, nil, fmt.Errorf("failure without a message: %s", end)
	}
	return historypb.FeedStatus_FEED_STATUS_FAILED, &message, nil
}
