package sessionclient

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strconv"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
)

const (
	maxTurnCount      = 100
	turnEventPageSize = 1000
	turnCursorVersion = 1
)

// Source identifies the backend that produced one independently observed part of a projection.
type Source string

const (
	SourceDispatcher Source = "dispatcher"
	SourceHistory    Source = "history"
)

// SourceKind describes what a backend read established.
type SourceKind string

const (
	SourceObserved    SourceKind = "observed"
	SourceNotObserved SourceKind = "not_observed"
	SourceUnavailable SourceKind = "unavailable"
	SourcePartial     SourceKind = "partial"
)

// SourcePart preserves the result of one backend read without treating an absent record or failed
// source as an empty successful value. Value is ProtoJSON from the owning service.
type SourcePart struct {
	Kind       SourceKind      `json:"kind"`
	Source     Source          `json:"source"`
	ObservedAt *time.Time      `json:"observed_at,omitempty"`
	Value      json.RawMessage `json:"value,omitempty"`
	Error      *SourceError    `json:"error,omitempty"`
}

// SessionSnapshot composes the durable Dispatcher Session and retained History summary. It makes
// no live-environment lookup and owns neither credentials nor authorization.
type SessionSnapshot struct {
	SessionID uuid.UUID  `json:"session_id"`
	Session   SourcePart `json:"session"`
	History   SourcePart `json:"history"`
}

// GetSession reads both independently owned sources, retaining one source's result when the other
// is absent or unavailable.
func GetSession(
	ctx context.Context,
	dispatcher dispatcherpb.SessionDispatcherClient,
	history historypb.HistoryServiceClient,
	sessionID uuid.UUID,
) (SessionSnapshot, error) {
	snapshot := SessionSnapshot{
		SessionID: sessionID,
		Session:   SourcePart{Kind: SourceNotObserved, Source: SourceDispatcher},
		History:   SourcePart{Kind: SourceNotObserved, Source: SourceHistory},
	}
	session, sessionErr := dispatcher.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: sessionID.String()})
	if sessionErr != nil {
		if ctx.Err() != nil {
			return snapshot, status.FromContextError(ctx.Err()).Err()
		}
		if status.Code(sessionErr) != codes.NotFound {
			snapshot.Session.Kind = SourceUnavailable
			snapshot.Session.Error = sourceFailure(sessionErr)
		}
	} else if session == nil || session.GetSession() == nil {
		snapshot.Session.Kind = SourceUnavailable
		snapshot.Session.Error = &SourceError{Code: codes.DataLoss.String(), Message: "Dispatcher returned no Session"}
	} else if session.GetSession().GetSessionId() != sessionID.String() {
		snapshot.Session.Kind = SourceUnavailable
		snapshot.Session.Error = &SourceError{Code: codes.DataLoss.String(), Message: "Dispatcher returned a different Session"}
	} else {
		value, marshalErr := protojson.Marshal(session.GetSession())
		if marshalErr != nil {
			return snapshot, fmt.Errorf("marshal Dispatcher Session: %w", marshalErr)
		}
		now := time.Now().UTC()
		snapshot.Session = SourcePart{Kind: SourceObserved, Source: SourceDispatcher, ObservedAt: &now, Value: value}
	}

	summary, historyErr := history.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: sessionID.String()})
	if historyErr != nil {
		if ctx.Err() != nil {
			return snapshot, status.FromContextError(ctx.Err()).Err()
		}
		if status.Code(historyErr) != codes.NotFound {
			snapshot.History.Kind = SourceUnavailable
			snapshot.History.Error = sourceFailure(historyErr)
		}
	} else if summary == nil {
		snapshot.History.Kind = SourceUnavailable
		snapshot.History.Error = &SourceError{Code: codes.DataLoss.String(), Message: "History returned no summary"}
	} else if summary.GetSessionId() != sessionID.String() {
		snapshot.History.Kind = SourceUnavailable
		snapshot.History.Error = &SourceError{Code: codes.DataLoss.String(), Message: "History returned a summary for a different Session"}
	} else {
		value, marshalErr := protojson.Marshal(summary)
		if marshalErr != nil {
			return snapshot, fmt.Errorf("marshal History Session summary: %w", marshalErr)
		}
		now := time.Now().UTC()
		snapshot.History = SourcePart{Kind: SourceObserved, Source: SourceHistory, ObservedAt: &now, Value: value}
	}
	if ctx.Err() != nil {
		return snapshot, status.FromContextError(ctx.Err()).Err()
	}
	return snapshot, nil
}

// OpaqueID is a nonempty source-owned identifier. It deliberately makes no UUID or ordering
// assumption.
type OpaqueID string

// TurnStatus is the protobuf TurnStatus enum's ProtoJSON spelling.
type TurnStatus string

// HistoryCursorRange preserves protobuf uint64 JSON boundaries while keeping cursor values typed.
type HistoryCursorRange struct {
	First uint64 `json:"first"`
	Last  uint64 `json:"last"`
}

func (r HistoryCursorRange) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		First string `json:"first"`
		Last  string `json:"last"`
	}{
		First: strconv.FormatUint(r.First, 10),
		Last:  strconv.FormatUint(r.Last, 10),
	})
}

func (r *HistoryCursorRange) UnmarshalJSON(data []byte) error {
	var encoded struct {
		First string `json:"first"`
		Last  string `json:"last"`
	}
	if err := json.Unmarshal(data, &encoded); err != nil {
		return err
	}
	first, err := strconv.ParseUint(encoded.First, 10, 64)
	if err != nil {
		return fmt.Errorf("parse first History cursor: %w", err)
	}
	last, err := strconv.ParseUint(encoded.Last, 10, 64)
	if err != nil {
		return fmt.Errorf("parse last History cursor: %w", err)
	}
	if first == 0 || first > last {
		return errors.New("invalid History cursor range")
	}
	r.First, r.Last = first, last
	return nil
}

// HistoryCoverage describes the retained prefix covered by this projection. ThroughCursor is a
// decimal uint64, matching protobuf JSON's boundary behavior; ResumeToken is a session-scoped page
// cursor and carries no authority.
type HistoryCoverage struct {
	Source        Source               `json:"source"`
	ThroughCursor string               `json:"through_cursor,omitempty"`
	Gaps          []HistoryCursorRange `json:"gaps"`
	ResumeToken   string               `json:"resume_token,omitempty"`
	Error         *SourceError         `json:"error,omitempty"`
}

// TurnPage is a bounded page of compact retained turns.
type TurnPage struct {
	HistoryCoverage
	SessionID  uuid.UUID  `json:"session_id"`
	Turns      []TurnView `json:"turns"`
	NextCursor string     `json:"next_cursor,omitempty"`
}

// ReadTurnsRequest selects a Session and bounds the number of returned turns. PageCursor, when
// supplied, is an opaque hint scoped to SessionID; it is never an authorization credential.
type ReadTurnsRequest struct {
	SessionID  uuid.UUID
	Count      uint32
	PageCursor string
}

type turnPageCursor struct {
	Version     int    `json:"v"`
	SessionID   string `json:"session_id"`
	AfterCursor uint64 `json:"after_cursor"`
	Through     uint64 `json:"through_cursor"`
}

// ReadTurns pages through ReadSessionEvents, compacting only events from the named retained
// Session. A page ends at a completed-turn boundary unless it reaches the pinned History tail;
// that final page can include an unfinished turn with no inferred terminal status.
func ReadTurns(ctx context.Context, history historypb.HistoryServiceClient, request ReadTurnsRequest) (TurnPage, error) {
	if request.Count < 1 || request.Count > maxTurnCount {
		return TurnPage{}, status.Error(codes.InvalidArgument, "count must be between 1 and 100")
	}
	sessionID := request.SessionID
	page := TurnPage{
		HistoryCoverage: HistoryCoverage{Source: SourceHistory, Gaps: []HistoryCursorRange{}},
		SessionID:       sessionID,
		Turns:           []TurnView{},
	}
	var cursor turnPageCursor
	var err error
	if request.PageCursor != "" {
		cursor, err = decodeTurnPageCursor(request.PageCursor, sessionID)
		if err != nil {
			return TurnPage{}, status.Error(codes.InvalidArgument, "invalid page cursor for this Session")
		}
	}

	// The first read establishes the retained prefix for this pagination snapshot. On later pages,
	// the same check confirms that untrusted cursor bounds remain inside the Session's current
	// retained prefix; new events after the pinned tail are not mixed into older pages.
	probe, err := history.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{
		SessionId: sessionID.String(), AfterCursor: 0, Limit: 1,
	})
	if err != nil {
		if ctx.Err() != nil {
			return page, status.FromContextError(ctx.Err()).Err()
		}
		page.Error = sourceFailure(err)
		return page, nil
	}
	if probe == nil {
		page.Error = &SourceError{Code: codes.DataLoss.String(), Message: "History returned an empty response"}
		return page, nil
	}
	if request.PageCursor == "" {
		cursor = turnPageCursor{Version: turnCursorVersion, SessionID: sessionID.String(), Through: probe.GetLastCursor()}
	} else if cursor.Through > probe.GetLastCursor() || cursor.AfterCursor > cursor.Through {
		return TurnPage{}, status.Error(codes.InvalidArgument, "page cursor is outside retained History")
	}

	if err := validateTurnPageCursor(ctx, history, sessionID, cursor); err != nil {
		if status.Code(err) == codes.InvalidArgument {
			return TurnPage{}, err
		}
		if ctx.Err() != nil {
			return page, status.FromContextError(ctx.Err()).Err()
		}
		page.Error = sourceFailure(err)
		return page, nil
	}
	projection := TurnProjection{}
	after := cursor.AfterCursor
	lastComplete := cursor.AfterCursor
	coveredThrough := cursor.AfterCursor
	var contextErr error
	for after < cursor.Through {
		pageAfter := after
		events, readErr := history.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{
			SessionId: sessionID.String(), AfterCursor: after, Limit: turnEventPageSize,
		})
		if readErr != nil {
			if ctx.Err() != nil {
				contextErr = status.FromContextError(ctx.Err()).Err()
				readErr = contextErr
			}
			page.Error = sourceFailure(readErr)
			break
		}
		if events == nil {
			page.Error = &SourceError{Code: codes.DataLoss.String(), Message: "History returned an empty response"}
			break
		}
		if events.GetLastCursor() < cursor.Through {
			page.Error = &SourceError{Code: codes.DataLoss.String(), Message: "retained History cursor moved behind the page snapshot"}
			break
		}
		entries := events.GetEntries()
		lastScanned := after
		stop := false
		for _, entry := range entries {
			if entry == nil || entry.GetCursor() <= lastScanned {
				page.Error = &SourceError{Code: codes.DataLoss.String(), Message: "History returned entries out of cursor order"}
				stop = true
				break
			}
			if entry.GetCursor() > cursor.Through {
				break
			}
			completed, err := projection.Add(entry)
			if err != nil {
				page.Error = &SourceError{Code: codes.DataLoss.String(), Message: "retained History contains an incomplete turn projection"}
				stop = true
				break
			}
			lastScanned = entry.GetCursor()
			coveredThrough = lastScanned
			if completed != nil {
				page.Turns = append(page.Turns, *completed)
				lastComplete = lastScanned
				if uint32(len(page.Turns)) == request.Count {
					if lastScanned < cursor.Through {
						page.NextCursor = encodeTurnPageCursor(turnPageCursor{
							Version: turnCursorVersion, SessionID: sessionID.String(), AfterCursor: lastScanned, Through: cursor.Through,
						})
						page.ResumeToken = page.NextCursor
					}
					stop = true
					break
				}
			}
		}
		if stop {
			if page.Error != nil {
				if lastComplete < cursor.Through {
					page.NextCursor = encodeTurnPageCursor(turnPageCursor{
						Version: turnCursorVersion, SessionID: sessionID.String(), AfterCursor: lastComplete, Through: cursor.Through,
					})
					page.ResumeToken = page.NextCursor
				}
			}
			addCoverageGaps(&page.HistoryCoverage, events.GetRemoved(), pageAfter, coveredThrough)
			break
		}
		if len(entries) >= turnEventPageSize && lastScanned > after {
			addCoverageGaps(&page.HistoryCoverage, events.GetRemoved(), pageAfter, lastScanned)
			after = lastScanned
			continue
		}
		// A short History page covers the rest of the snapshot, including explicitly removed
		// cursor ranges. Settled deltas are represented by their retained completion event and are
		// not reported as missing History.
		coveredThrough = cursor.Through
		addCoverageGaps(&page.HistoryCoverage, events.GetRemoved(), pageAfter, cursor.Through)
		after = cursor.Through
	}

	page.ThroughCursor = strconv.FormatUint(coveredThrough, 10)
	if page.Error != nil && page.NextCursor == "" && lastComplete < cursor.Through {
		page.NextCursor = encodeTurnPageCursor(turnPageCursor{
			Version: turnCursorVersion, SessionID: sessionID.String(), AfterCursor: lastComplete, Through: cursor.Through,
		})
		page.ResumeToken = page.NextCursor
	}
	if page.Error != nil || page.NextCursor == "" && after >= cursor.Through {
		if partial := projection.Partial(); partial != nil {
			page.Turns = append(page.Turns, *partial)
		}
	}
	return page, contextErr
}

func validateTurnPageCursor(
	ctx context.Context,
	history historypb.HistoryServiceClient,
	sessionID uuid.UUID,
	cursor turnPageCursor,
) error {
	if cursor.Version != turnCursorVersion || cursor.SessionID != sessionID.String() || cursor.AfterCursor > cursor.Through {
		return status.Error(codes.InvalidArgument, "invalid page cursor for this Session")
	}
	if cursor.AfterCursor == 0 {
		return nil
	}
	// Page cursors are untrusted hints. Verify that the after position is still retained as a
	// turn boundary, so editing the base64 payload cannot start a page in the middle of a turn.
	page, err := history.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{
		SessionId: sessionID.String(), AfterCursor: cursor.AfterCursor - 1, Limit: 1,
	})
	if err != nil {
		return err
	}
	if page == nil || len(page.GetEntries()) != 1 || page.GetEntries()[0].GetCursor() != cursor.AfterCursor ||
		page.GetEntries()[0].GetEvent().GetTurnCompleted() == nil {
		return status.Error(codes.DataLoss, "page cursor is no longer a retained turn boundary")
	}
	return nil
}

func decodeTurnPageCursor(token string, sessionID uuid.UUID) (turnPageCursor, error) {
	if len(token) > 4096 {
		return turnPageCursor{}, errors.New("page cursor is too long")
	}
	data, err := base64.RawURLEncoding.DecodeString(token)
	if err != nil {
		return turnPageCursor{}, err
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var cursor turnPageCursor
	if err := decoder.Decode(&cursor); err != nil {
		return turnPageCursor{}, err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return turnPageCursor{}, errors.New("page cursor has trailing data")
	}
	if cursor.SessionID != sessionID.String() || cursor.Version != turnCursorVersion || cursor.AfterCursor > cursor.Through {
		return turnPageCursor{}, errors.New("page cursor does not match Session")
	}
	return cursor, nil
}

func encodeTurnPageCursor(cursor turnPageCursor) string {
	data, _ := json.Marshal(cursor)
	return base64.RawURLEncoding.EncodeToString(data)
}

func addCoverageGaps(coverage *HistoryCoverage, ranges []*historypb.CursorRange, after, through uint64) {
	for _, cursorRange := range ranges {
		if cursorRange == nil || cursorRange.GetLast() <= after || cursorRange.GetFirst() > through {
			continue
		}
		first := cursorRange.GetFirst()
		if first <= after {
			first = after + 1
		}
		last := cursorRange.GetLast()
		if last > through {
			last = through
		}
		if first <= last {
			coverage.Gaps = append(coverage.Gaps, HistoryCursorRange{First: first, Last: last})
		}
	}
}
