// Package historydata reads the generated History Service API and validates its cursor windows.
package historydata

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/settledcursors"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

var (
	ErrNotFound         = errors.New("History Session not found")
	ErrScopeChanged     = errors.New("History projection scope changed")
	ErrEvidenceNotFound = errors.New("History entity evidence not found")
	ErrInvalidPage      = errors.New("History Service returned an invalid page")
	ErrInvalidSessionID = errors.New("History Service returned an invalid Session ID")
	ErrCommandConflict  = errors.New("command ID was already admitted with different work")
)

// Reader is the generated History Service read boundary. The concrete client is
// historyclient.Client; tests can provide the same generated-message contract.
type Reader interface {
	ReadSessionEvents(context.Context, *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error)
	ReadSessionObservations(context.Context, *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error)
	GetSessionSummary(context.Context, *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error)
	BatchGetSessionSummaries(context.Context, *historypb.BatchGetSessionSummariesRequest) (*historypb.BatchGetSessionSummariesResponse, error)
	ReadThreadEntities(context.Context, *historypb.ReadThreadEntitiesRequest) (*historypb.ReadThreadEntitiesResponse, error)
	ReadEntityEvidence(context.Context, *historypb.ReadEntityEvidenceRequest) (*historypb.ReadEntityEvidenceResponse, error)
	ReadNativeFrames(context.Context, *historypb.ReadNativeFramesRequest) (*historypb.ReadNativeFramesResponse, error)
	FindSessions(context.Context, *historypb.FindSessionsRequest) (*historypb.FindSessionsResponse, error)
}

type Store struct{ reader Reader }

func New(reader Reader) (*Store, error) {
	if reader == nil {
		return nil, errors.New("History Service reader is required")
	}
	return &Store{reader: reader}, nil
}

type FeedEnd struct{ Message string }

type FeedSnapshot struct {
	Status   historypb.FeedStatus
	Attached *runnerpb.Attached
	End      *FeedEnd
}

type FoldSummary struct {
	Present             bool
	Sandbox             string
	SandboxUID          string
	ProjectionEpoch     *string
	FoldedThrough       uint64
	FirstEventAt        *time.Time
	LastEventAt         *time.Time
	LastModelActivityAt *time.Time
	IdleSince           *time.Time
	LastTurnStatus      *int32
	Feed                *FeedSnapshot
}

func (s *Store) GetSummary(ctx context.Context, id uuid.UUID) (FoldSummary, error) {
	message, err := s.reader.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: id.String()})
	if status.Code(err) == codes.NotFound {
		return FoldSummary{}, nil
	}
	if err != nil {
		return FoldSummary{}, err
	}
	return foldSummary(message)
}

func (s *Store) BatchGetSummaries(ctx context.Context, ids []uuid.UUID) (map[uuid.UUID]FoldSummary, error) {
	result := make(map[uuid.UUID]FoldSummary, len(ids))
	for start := 0; start < len(ids); start += 200 {
		end := min(start+200, len(ids))
		batch := make([]string, 0, end-start)
		for _, id := range ids[start:end] {
			batch = append(batch, id.String())
			result[id] = FoldSummary{}
		}
		response, err := s.reader.BatchGetSessionSummaries(ctx, &historypb.BatchGetSessionSummariesRequest{SessionIds: batch})
		if err != nil {
			return nil, err
		}
		for _, message := range response.GetSummaries() {
			id, err := canonicalUUID(message.GetSessionId())
			if err != nil {
				return nil, err
			}
			view, err := foldSummary(message)
			if err != nil {
				return nil, err
			}
			result[id] = view
		}
	}
	return result, nil
}

func (s *Store) FindSessions(ctx context.Context, sandboxName, sandboxNameContains *string) (map[uuid.UUID]string, error) {
	request := &historypb.FindSessionsRequest{}
	switch {
	case sandboxName != nil:
		request.Sandbox = &historypb.FindSessionsRequest_SandboxName{SandboxName: *sandboxName}
	case sandboxNameContains != nil:
		request.Sandbox = &historypb.FindSessionsRequest_SandboxNameContains{SandboxNameContains: *sandboxNameContains}
	default:
		return nil, errors.New("History Session lookup requires one Sandbox filter")
	}
	response, err := s.reader.FindSessions(ctx, request)
	if err != nil {
		return nil, err
	}
	result := make(map[uuid.UUID]string, len(response.GetSessions()))
	for _, item := range response.GetSessions() {
		id, err := canonicalUUID(item.GetSessionId())
		if err != nil {
			return nil, err
		}
		if item.GetSandbox() == nil || item.GetSandbox().GetName() == "" {
			return nil, fmt.Errorf("%w: Session %s has no Sandbox identity", ErrInvalidPage, id)
		}
		result[id] = item.GetSandbox().GetName()
	}
	return result, nil
}

func canonicalUUID(value string) (uuid.UUID, error) {
	id, err := uuid.Parse(value)
	if err != nil || id.String() != value {
		return uuid.Nil, fmt.Errorf("%w: %q", ErrInvalidSessionID, value)
	}
	return id, nil
}

func foldSummary(message *historypb.SessionSummary) (FoldSummary, error) {
	if message == nil {
		return FoldSummary{}, fmt.Errorf("%w: empty summary", ErrInvalidPage)
	}
	view := FoldSummary{Present: true, FoldedThrough: message.GetFoldedThrough()}
	if message.ProjectionEpoch != nil {
		epoch := message.GetProjectionEpoch()
		view.ProjectionEpoch = &epoch
	}
	if sandbox := message.GetSandbox(); sandbox != nil {
		view.Sandbox = sandbox.GetName()
		view.SandboxUID = sandbox.GetUid()
	}
	view.FirstEventAt = timestamp(message.GetFirstEventAt())
	view.LastEventAt = timestamp(message.GetLastEventAt())
	view.LastModelActivityAt = timestamp(message.GetLastModelActivityAt())
	view.IdleSince = timestamp(message.GetIdleSince())
	if message.LastTurnStatus != nil {
		value := int32(message.GetLastTurnStatus())
		view.LastTurnStatus = &value
	}
	if message.GetAttached() != nil {
		feed := &FeedSnapshot{Status: message.GetFeedStatus(), Attached: message.GetAttached()}
		switch message.GetFeedStatus() {
		case historypb.FeedStatus_FEED_STATUS_ACTIVE:
		case historypb.FeedStatus_FEED_STATUS_ENDED:
			feed.End = &FeedEnd{}
		case historypb.FeedStatus_FEED_STATUS_FAILED:
			feed.End = &FeedEnd{Message: message.GetFeedError()}
		default:
			return FoldSummary{}, fmt.Errorf("%w: attached Session summary has unspecified feed status", ErrInvalidPage)
		}
		view.Feed = feed
	}
	return view, nil
}

func timestamp(value *timestamppb.Timestamp) *time.Time {
	if value == nil || !value.IsValid() {
		return nil
	}
	converted := value.AsTime().UTC()
	return &converted
}

type ArchivedObservation struct {
	Cursor string `json:"cursor"`
	Kind   string `json:"kind"`
}

type ObservationPage struct {
	Observations     []ArchivedObservation `json:"observations" nullable:"false"`
	NextBeforeCursor *string               `json:"next_before_cursor"`
	NextAfterCursor  *string               `json:"next_after_cursor"`
}

func (s *Store) ReadWatermark(ctx context.Context, id uuid.UUID) (uint64, error) {
	page, err := s.reader.ReadSessionObservations(ctx, &historypb.ReadSessionObservationsRequest{SessionId: id.String(), Limit: 1})
	if status.Code(err) == codes.NotFound {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	return page.GetLastCursor(), nil
}

func (s *Store) Observations(ctx context.Context, id uuid.UUID, before, after *uint64, limit int) (ObservationPage, error) {
	if limit < 1 || limit > 200 || (before != nil && after != nil) {
		return ObservationPage{}, fmt.Errorf("invalid chronological observation page bounds")
	}
	request := &historypb.ReadSessionObservationsRequest{SessionId: id.String(), Limit: uint32(limit)}
	if before != nil {
		request.BeforeCursor = before
	}
	if after != nil {
		request.AfterCursor = after
	}
	page, err := s.reader.ReadSessionObservations(ctx, request)
	if err != nil {
		return ObservationPage{}, err
	}
	observations := page.GetObservations()
	cursors := make([]uint64, 0, len(observations))
	for _, row := range observations {
		cursors = append(cursors, row.GetCursor())
	}
	settled := page.GetSettled()
	var within, shortOK bool
	if after != nil {
		walk := append([]uint64{*after}, cursors...)
		last := walk[len(walk)-1]
		within = len(cursors) == 0 || cursors[len(cursors)-1] <= page.GetLastCursor()
		shortOK = settledcursors.NextStored(last, settled) > page.GetLastCursor()
	} else {
		end := page.GetLastCursor()
		if before != nil && (*before == 0 || *before <= end) {
			if *before == 0 {
				end = 0
			} else {
				end = *before - 1
			}
		}
		within = len(cursors) == 0 || (cursors[len(cursors)-1] <= end && end < settledcursors.NextStored(cursors[len(cursors)-1], settled))
		shortOK = (len(cursors) > 0 && cursors[0] == 1) || (len(cursors) == 0 && end < 1)
	}
	contiguous := true
	for index := 1; index < len(cursors); index++ {
		if cursors[index] != settledcursors.NextStored(cursors[index-1], settled) {
			contiguous = false
			break
		}
	}
	if !within || !contiguous || (len(cursors) != limit && !shortOK) {
		return ObservationPage{}, fmt.Errorf("%w: invalid service observation page", ErrInvalidPage)
	}
	result := ObservationPage{Observations: make([]ArchivedObservation, 0, len(observations))}
	for _, row := range observations {
		result.Observations = append(result.Observations, ArchivedObservation{Cursor: fmt.Sprint(row.GetCursor()), Kind: row.GetKind()})
	}
	if len(observations) > 0 && observations[0].GetCursor() > 1 {
		value := fmt.Sprint(observations[0].GetCursor())
		result.NextBeforeCursor = &value
	}
	if len(observations) > 0 && settledcursors.NextStored(observations[len(observations)-1].GetCursor(), settled) <= page.GetLastCursor() {
		value := fmt.Sprint(observations[len(observations)-1].GetCursor())
		result.NextAfterCursor = &value
	}
	return result, nil
}

type ObservationEntry struct {
	Cursor string         `json:"cursor"`
	Entry  map[string]any `json:"entry"`
}

func (s *Store) ObservationEntry(ctx context.Context, id uuid.UUID, cursor uint64, foldedThrough uint64) (*ObservationEntry, error) {
	if cursor < 1 {
		return nil, nil
	}
	watermark, err := s.ReadWatermark(ctx, id)
	if err != nil {
		return nil, err
	}
	if cursor > watermark {
		if cursor <= foldedThrough {
			return nil, fmt.Errorf("History Service has not committed cursor %d used by its fold", cursor)
		}
		return nil, nil
	}
	page, err := s.reader.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{SessionId: id.String(), AfterCursor: cursor - 1, Limit: 1})
	if err != nil {
		return nil, err
	}
	if settledcursors.NextStored(cursor-1, settledcursors.Unstored(page)) != cursor {
		return nil, nil
	}
	if len(page.GetEntries()) != 1 || page.GetEntries()[0].GetCursor() != cursor {
		return nil, fmt.Errorf("%w: History Service did not return requested cursor %d", ErrInvalidPage, cursor)
	}
	encoded, err := (protojson.MarshalOptions{}).Marshal(page.GetEntries()[0])
	if err != nil {
		return nil, err
	}
	var entry map[string]any
	if err := json.Unmarshal(encoded, &entry); err != nil {
		return nil, err
	}
	return &ObservationEntry{Cursor: fmt.Sprint(cursor), Entry: entry}, nil
}

// Events returns a bounded contiguous runner-event window, treating settled and removed cursor
// ranges as covered. A first NOT_FOUND means the runner has not pushed the Session yet.
func (s *Store) Events(ctx context.Context, id uuid.UUID, after uint64, limit int) ([]*eventlogpb.EventEntry, error) {
	if limit < 0 {
		return nil, errors.New("event limit must be non-negative")
	}
	result := make([]*eventlogpb.EventEntry, 0, limit)
	cursor := after
	var through *uint64
	for len(result) < limit {
		requested := min(limit-len(result), 1000)
		if through != nil {
			if cursor > *through {
				return nil, fmt.Errorf("%w: History Service regressed behind cursor", ErrInvalidPage)
			}
			remaining := *through - cursor
			if remaining < uint64(requested) {
				requested = int(remaining)
			}
			if requested == 0 {
				break
			}
		}
		page, err := s.reader.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{
			SessionId: id.String(), AfterCursor: cursor, Limit: uint32(requested),
		})
		if status.Code(err) == codes.NotFound && through == nil {
			return result, nil
		}
		if err != nil {
			return nil, err
		}
		if through == nil {
			watermark := page.GetLastCursor()
			through = &watermark
		}
		if page.GetLastCursor() < *through || cursor > page.GetLastCursor() {
			return nil, fmt.Errorf("%w: History Service regressed behind cursor", ErrInvalidPage)
		}
		entries := page.GetEntries()
		unstored := settledcursors.Unstored(page)
		if len(entries) > requested {
			return nil, fmt.Errorf("%w: History Service exceeded requested page size", ErrInvalidPage)
		}
		previous := cursor
		for _, entry := range entries {
			if entry.GetCursor() != settledcursors.NextStored(previous, unstored) {
				return nil, fmt.Errorf("%w: History Service page has an uncovered cursor gap", ErrInvalidPage)
			}
			previous = entry.GetCursor()
		}
		if len(entries) > 0 && entries[len(entries)-1].GetCursor() > page.GetLastCursor() {
			return nil, fmt.Errorf("%w: History Service entry exceeds watermark", ErrInvalidPage)
		}
		if len(entries) == 0 {
			if settledcursors.NextStored(cursor, unstored) <= *through {
				return nil, fmt.Errorf("%w: History Service omitted entries from published prefix", ErrInvalidPage)
			}
			break
		}
		prior := len(result)
		for _, entry := range entries {
			if entry.GetCursor() <= *through {
				result = append(result, entry)
			}
		}
		if len(result) == prior {
			break
		}
		cursor = result[len(result)-1].GetCursor()
	}
	return result, nil
}

type Scope struct {
	ProjectionEpoch string
	ThroughCursor   uint64
}

func (s *Store) CurrentScope(ctx context.Context, id uuid.UUID) (*Scope, error) {
	message, err := s.reader.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: id.String()})
	if status.Code(err) == codes.NotFound {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if message.ProjectionEpoch == nil {
		return nil, nil
	}
	return &Scope{ProjectionEpoch: message.GetProjectionEpoch(), ThroughCursor: message.GetFoldedThrough()}, nil
}

type EvidenceObservation struct {
	ObservationCursor string `json:"observation_cursor"`
	HasNative         bool   `json:"has_native"`
}

type EvidencePage struct {
	Observations    []EvidenceObservation `json:"observations" nullable:"false"`
	NextAfterCursor *string               `json:"next_after_cursor"`
}

func (s *Store) Evidence(ctx context.Context, id uuid.UUID, epoch, kind, entity string, after uint64, limit int) (EvidencePage, error) {
	if limit < 1 || limit > 200 {
		return EvidencePage{}, errors.New("evidence page size must be between 1 and 200")
	}
	page, err := s.reader.ReadEntityEvidence(ctx, &historypb.ReadEntityEvidenceRequest{
		SessionId: id.String(), ProjectionEpoch: epoch,
		Entity: &historypb.EntityKey{EntityKind: kind, EntityId: entity}, AfterCursor: after, Limit: uint32(limit),
	})
	if status.Code(err) == codes.FailedPrecondition {
		return EvidencePage{}, ErrScopeChanged
	}
	if status.Code(err) == codes.NotFound {
		return EvidencePage{}, ErrEvidenceNotFound
	}
	if err != nil {
		return EvidencePage{}, err
	}
	result := EvidencePage{Observations: make([]EvidenceObservation, 0, len(page.GetObservations()))}
	for _, row := range page.GetObservations() {
		result.Observations = append(result.Observations, EvidenceObservation{
			ObservationCursor: fmt.Sprint(row.GetObservationCursor()), HasNative: row.GetHasNative(),
		})
	}
	if page.NextAfterCursor != nil {
		value := fmt.Sprint(page.GetNextAfterCursor())
		result.NextAfterCursor = &value
	}
	return result, nil
}

type NativeFrame struct {
	SourceSequence string         `json:"source_sequence"`
	Availability   string         `json:"availability"`
	Entry          map[string]any `json:"entry"`
}

type NativeFramePage struct {
	Frames            []NativeFrame `json:"frames" nullable:"false"`
	NextAfterSequence *string       `json:"next_after_sequence"`
}

func (s *Store) NativeFrames(ctx context.Context, id uuid.UUID, epoch, kind, entity string, observation, after uint64, limit int) (NativeFramePage, error) {
	if limit < 1 || limit > 200 {
		return NativeFramePage{}, errors.New("native frame page size must be between 1 and 200")
	}
	page, err := s.reader.ReadNativeFrames(ctx, &historypb.ReadNativeFramesRequest{
		SessionId: id.String(), ProjectionEpoch: epoch,
		Entity:            &historypb.EntityKey{EntityKind: kind, EntityId: entity},
		ObservationCursor: observation, AfterSequence: after, Limit: uint32(limit),
	})
	if status.Code(err) == codes.FailedPrecondition {
		return NativeFramePage{}, ErrScopeChanged
	}
	if status.Code(err) == codes.NotFound {
		return NativeFramePage{}, ErrEvidenceNotFound
	}
	if err != nil {
		return NativeFramePage{}, err
	}
	result := NativeFramePage{Frames: make([]NativeFrame, 0, len(page.GetFrames()))}
	for _, frame := range page.GetFrames() {
		item := NativeFrame{SourceSequence: fmt.Sprint(frame.GetSourceSequence()), Availability: "unavailable"}
		if frame.GetEntry() != nil {
			item.Availability = "present"
			encoded, err := (protojson.MarshalOptions{}).Marshal(frame.GetEntry())
			if err != nil {
				return NativeFramePage{}, err
			}
			if err := json.Unmarshal(encoded, &item.Entry); err != nil {
				return NativeFramePage{}, err
			}
		}
		result.Frames = append(result.Frames, item)
	}
	if page.NextAfterSequence != nil {
		value := fmt.Sprint(page.GetNextAfterSequence())
		result.NextAfterSequence = &value
	}
	return result, nil
}

// AdmittedCommand finds a command's durable History admission before deciding whether the exact
// request is an idempotent archived retry. The projection locates the cursor; the immutable event
// log remains the authority for the original Command body.
func (s *Store) AdmittedCommand(ctx context.Context, id uuid.UUID, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	if command == nil || command.GetCommandId() == "" {
		return nil, errors.New("command and stable command ID are required")
	}
	page, err := s.reader.ReadThreadEntities(ctx, &historypb.ReadThreadEntitiesRequest{
		SessionId: id.String(),
		Window: &historypb.ReadThreadEntitiesRequest_Keys{Keys: &historypb.EntityKeys{Keys: []*historypb.EntityKey{{
			EntityKind: "command", EntityId: command.GetCommandId(),
		}}}},
	})
	if status.Code(err) == codes.FailedPrecondition || status.Code(err) == codes.NotFound {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if len(page.GetEntities()) == 0 {
		return nil, nil
	}
	if len(page.GetEntities()) != 1 {
		return nil, fmt.Errorf("%w: History Service returned duplicate command entities", ErrInvalidPage)
	}
	var view struct {
		EntityKind string `json:"entity_kind"`
		EntityID   string `json:"entity_id"`
		Cursor     uint64 `json:"cursor"`
	}
	if err := json.Unmarshal([]byte(page.GetEntities()[0].GetViewJson()), &view); err != nil {
		return nil, fmt.Errorf("decode History command entity: %w", err)
	}
	if view.EntityKind != "command" || view.EntityID != command.GetCommandId() || view.Cursor == 0 {
		return nil, fmt.Errorf("%w: command entity did not identify its admission cursor", ErrInvalidPage)
	}
	entries, err := s.reader.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{
		SessionId: id.String(), AfterCursor: view.Cursor - 1, Limit: 1,
	})
	if err != nil {
		return nil, err
	}
	if settledcursors.NextStored(view.Cursor-1, settledcursors.Unstored(entries)) != view.Cursor {
		return nil, errors.New("command summary has no archived admission")
	}
	if len(entries.GetEntries()) != 1 {
		return nil, fmt.Errorf("%w: command admission page did not contain exactly one event", ErrInvalidPage)
	}
	entry := entries.GetEntries()[0]
	if entry.GetCursor() != view.Cursor || entry.GetOrigin().GetSequence() != view.Cursor {
		return nil, fmt.Errorf("%w: History Service returned a different command cursor", ErrInvalidPage)
	}
	admitted := entry.GetEvent().GetCommandAdmitted()
	if admitted == nil || admitted.GetCommand().GetCommandId() != command.GetCommandId() {
		return nil, errors.New("command summary has no matching archived admission")
	}
	if !proto.Equal(admitted.GetCommand(), command) {
		return nil, fmt.Errorf("%w: %q", ErrCommandConflict, command.GetCommandId())
	}
	return entry, nil
}
