package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/agentydragon/ducktape/agentplane/app/go/historydata"
	"github.com/agentydragon/ducktape/agentplane/app/go/httperror"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	environmentpb "github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
	"github.com/danielgtaylor/huma/v2"
	"github.com/google/uuid"
	gatewayruntime "github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	threadMaxPageSize = 200
)

type threadReadStore interface {
	GetThread(context.Context, uuid.UUID) (*threads.Thread, error)
	ListPage(context.Context, int, *threads.PageCursor, bool) (threads.Page, error)
	ArchivedCount(context.Context) (int, error)
	Rename(context.Context, uuid.UUID, *string) (threads.Thread, error)
	Archive(context.Context, uuid.UUID) (threads.Thread, error)
	Unarchive(context.Context, uuid.UUID) (threads.Thread, error)
}

type threadReadHistory interface {
	GetSummary(context.Context, uuid.UUID) (historydata.FoldSummary, error)
	BatchGetSummaries(context.Context, []uuid.UUID) (map[uuid.UUID]historydata.FoldSummary, error)
	FindSessions(context.Context, *string, *string, *string) (map[uuid.UUID]string, error)
}

type threadSandboxInventory interface {
	GetExact(context.Context, *environmentpb.TargetHandle) (*environmentpb.EnvironmentDescriptor, error)
}

type threadEnvironmentRuntimeStatusReader interface {
	EnvironmentRuntimeStatus(context.Context, *environmentpb.EnvironmentDescriptor) (*sessionpb.EnvironmentRuntimeStatus, error)
}

type (
	threadViewOutput struct{ Body threadView }
)

// threadView keeps the JSON contract of Python's ThreadView, including explicit nulls. The
// History Service owns the fold fields; PostgreSQL supplies only the operator metadata.
type threadView struct {
	ID                  uuid.UUID  `json:"id" format:"uuid"`
	Sandbox             string     `json:"sandbox"`
	SandboxUID          *string    `json:"sandbox_uid" nullable:"true"`
	Harness             *string    `json:"harness" enum:"HARNESS_CLAUDE,HARNESS_CODEX,HARNESS_CENTRAL_SAMPLER"`
	Model               *string    `json:"model"`
	CWD                 *string    `json:"cwd"`
	CreatedAt           time.Time  `json:"created_at"`
	Name                *string    `json:"name"`
	Archived            bool       `json:"archived"`
	LastCursor          uint64     `json:"last_cursor"`
	LastEventAt         *time.Time `json:"last_event_at"`
	LastModelActivityAt *time.Time `json:"last_model_activity_at"`
	IdleSince           *time.Time `json:"idle_since"`
	HarnessState        string     `json:"harness_state"`
	SetupState          *string    `json:"setup_state"`
	LaunchOutcome       *string    `json:"launch_outcome"`
	ActiveTurnID        *string    `json:"active_turn_id"`
	ReasoningEffort     *string    `json:"reasoning_effort"`
	FeedStatus          *string    `json:"feed_status" enum:"active,ended,failed"`
	LastTurnStatus      *string    `json:"last_turn_status"`
}

type threadPage struct {
	Threads       []threadView `json:"threads"`
	NextPageToken *string      `json:"next_page_token"`
}

type threadListing struct {
	Sandbox         *string `json:"sandbox"`
	SandboxUID      *string `json:"sandbox_uid"`
	Search          *string `json:"search"`
	IncludeArchived bool    `json:"include_archived"`
}

type threadPageToken struct {
	Listing   threadListing `json:"listing"`
	CreatedAt time.Time     `json:"created_at"`
	ID        uuid.UUID     `json:"id"`
}

type threadIDInput struct {
	ThreadID uuid.UUID `path:"thread_id" format:"uuid"`
}

type threadRenameInput struct {
	ThreadID uuid.UUID `path:"thread_id" format:"uuid"`
	Body     threadRenameBody
}

type threadRenameBody struct {
	Name    *string `json:"name" maxLength:"200"`
	Present bool    `json:"-"`
}

func (b *threadRenameBody) UnmarshalJSON(data []byte) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	for key := range fields {
		if key != "name" {
			return fmt.Errorf("unexpected field %q", key)
		}
	}
	raw, ok := fields["name"]
	if !ok {
		return errors.New("name is required")
	}
	b.Present = true
	if string(raw) == "null" {
		b.Name = nil
		return nil
	}
	return json.Unmarshal(raw, &b.Name)
}

// RegisterThreadReadRoutes adds the Thread read/mutation routes.
// History-owned reads are served by the History gateway; content and stream routes remain separate.
// The inventory and Environment Runtime status reader are required for the archive guard.
func RegisterThreadReadRoutes(
	server *HTTPServer,
	namespace string,
	store threadReadStore,
	history threadReadHistory,
	inventory threadSandboxInventory,
	environmentRuntime threadEnvironmentRuntimeStatusReader,
) error {
	api := server.ProtectedAPI()
	huma.Register[threadRenameInput, threadViewOutput](api, huma.Operation{
		OperationID: "renameThread", Method: http.MethodPatch, Path: "/threads/{thread_id}", Tags: []string{"threads"},
		Summary: "Set or clear an operator-managed Thread title",
	}, func(ctx context.Context, input *threadRenameInput) (*threadViewOutput, error) {
		if !input.Body.Present {
			return nil, huma.Error422UnprocessableEntity("name is required")
		}
		name := input.Body.Name
		if name != nil {
			trimmed := strings.TrimSpace(*name)
			if !utf8.ValidString(trimmed) || utf8.RuneCountInString(trimmed) > 200 {
				return nil, huma.Error422UnprocessableEntity("Thread name must be at most 200 characters")
			}
			if trimmed == "" {
				name = nil
			} else {
				name = &trimmed
			}
		}
		summary, err := readThreadSummary(ctx, history, input.ThreadID)
		if err != nil {
			return nil, threadReadHTTPError(err)
		}
		if !summary.Present {
			return nil, huma.Error404NotFound(fmt.Sprintf("no thread %s", input.ThreadID))
		}
		thread, err := store.Rename(ctx, input.ThreadID, name)
		if err != nil {
			return nil, threadReadHTTPError(err)
		}
		view, err := makeThreadView(thread, summary)
		if err != nil {
			return nil, threadReadHTTPError(err)
		}
		return &threadViewOutput{Body: view}, nil
	})
	huma.Register[threadIDInput, struct{}](api, huma.Operation{
		OperationID: "archiveThread", Method: http.MethodPost, Path: "/threads/{thread_id}/archive", Tags: []string{"threads"},
		Summary: "Hide an idle Thread from the default list", DefaultStatus: http.StatusNoContent,
	}, func(ctx context.Context, input *threadIDInput) (*struct{}, error) {
		thread, err := store.GetThread(ctx, input.ThreadID)
		if err != nil {
			return nil, threadReadHTTPError(err)
		}
		if thread == nil {
			return nil, huma.Error404NotFound(fmt.Sprintf("no thread %s", input.ThreadID))
		}
		summary, err := readThreadSummary(ctx, history, input.ThreadID)
		if err != nil {
			return nil, threadReadHTTPError(err)
		}
		if !summary.Present {
			return nil, huma.Error404NotFound(fmt.Sprintf("no thread %s", input.ThreadID))
		}
		if _, err := makeThreadView(*thread, summary); err != nil {
			return nil, threadReadHTTPError(err)
		}
		if err := ensureThreadArchivable(ctx, namespace, *thread, summary, inventory, environmentRuntime); err != nil {
			return nil, threadArchiveHTTPError(err)
		}
		if _, err := store.Archive(ctx, input.ThreadID); err != nil {
			return nil, threadReadHTTPError(err)
		}
		return nil, nil
	})
	huma.Register[threadIDInput, struct{}](api, huma.Operation{
		OperationID: "unarchiveThread", Method: http.MethodPost, Path: "/threads/{thread_id}/unarchive", Tags: []string{"threads"},
		Summary: "Show an archived Thread in the default list", DefaultStatus: http.StatusNoContent,
	}, func(ctx context.Context, input *threadIDInput) (*struct{}, error) {
		if _, err := store.Unarchive(ctx, input.ThreadID); err != nil {
			return nil, threadReadHTTPError(err)
		}
		return nil, nil
	})
	return nil
}

func registerThreadMetadataGateway(server *HTTPServer, service *appService) error {
	mux := gatewayruntime.NewServeMux()
	if err := apppb.RegisterAppServiceHandlerServer(context.Background(), mux, service); err != nil {
		return fmt.Errorf("register generated Thread Metadata gateway: %w", err)
	}
	gateway := http.StripPrefix("/upstream/app", mux)
	server.RegisterRawRoute("GET /upstream/app/v1/threads", gateway, false)
	server.RegisterRawRoute("GET /upstream/app/v1/threads/{thread_id}", gateway, false)
	// grpc-gateway's in-process registration supports unary calls only. Forward the same typed
	// watch through its standard stream encoder, without a local gRPC listener or transport.
	server.RegisterRawRoute("GET /upstream/app/v1/threads:watch", http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		ctx := gatewayruntime.NewServerMetadataContext(request.Context(), gatewayruntime.ServerMetadata{})
		_, marshaler := gatewayruntime.MarshalerForRequest(mux, request)
		watch, err := service.newThreadMetadataWatch(ctx)
		if watch != nil {
			defer watch.unsubscribe()
		}
		gatewayruntime.ForwardResponseStream(ctx, mux, marshaler, w, request, func() (proto.Message, error) {
			if err != nil {
				return nil, err
			}
			return watch.next()
		})
	}), false)
	return nil
}

func (s *appService) GetThreadMetadata(ctx context.Context, request *apppb.GetThreadMetadataRequest) (*apppb.ThreadMetadata, error) {
	id, err := uuid.Parse(request.GetThreadId())
	if err != nil || id.String() != request.GetThreadId() {
		return nil, status.Error(codes.InvalidArgument, "thread_id must be a canonical UUID")
	}
	thread, err := s.threadReader.GetThread(ctx, id)
	if err != nil {
		return nil, status.Error(codes.Internal, "read Thread metadata")
	}
	if thread == nil {
		return nil, status.Error(codes.NotFound, "Thread metadata not found")
	}
	return threadMetadataProto(*thread), nil
}

func (s *appService) ListThreadMetadata(ctx context.Context, request *apppb.ListThreadMetadataRequest) (*apppb.ListThreadMetadataResponse, error) {
	limit := int(request.GetLimit())
	if limit == 0 {
		limit = 50
	}
	if limit < 1 || limit > threadMaxPageSize {
		return nil, status.Errorf(codes.InvalidArgument, "limit must be between 1 and %d", threadMaxPageSize)
	}
	listing := threadListing{IncludeArchived: request.GetIncludeArchived()}
	if request.GetSandboxUid() != "" {
		listing.SandboxUID = stringPointer(request.GetSandboxUid())
	}
	var token *string
	if request.GetPageToken() != "" {
		value := request.GetPageToken()
		token = &value
	}
	if listing.SandboxUID != nil {
		if s.threadHistory == nil {
			return nil, status.Error(codes.Unavailable, "Thread history is unavailable")
		}
		page, err := listThreadViews(ctx, s.threadReader, s.threadHistory, listing, limit, token)
		if err != nil {
			if errors.Is(err, errMalformedThreadPageToken) || errors.Is(err, errMismatchedThreadListing) {
				return nil, status.Error(codes.InvalidArgument, err.Error())
			}
			return nil, status.Error(codes.Unavailable, "list Environment Threads")
		}
		response := &apppb.ListThreadMetadataResponse{Threads: make([]*apppb.ThreadMetadata, 0, len(page.Threads))}
		for _, thread := range page.Threads {
			response.Threads = append(response.Threads, &apppb.ThreadMetadata{
				ThreadId: thread.ID.String(), CreatedAt: timestamppb.New(thread.CreatedAt), Name: thread.Name, Archived: thread.Archived,
			})
		}
		if page.NextPageToken != nil {
			response.NextPageToken = *page.NextPageToken
		}
		// ArchivedCount follows the same Sandbox UID filter, independent of whether this page
		// includes archived Threads. Do not report the unrelated namespace-wide count.
		matching, err := s.threadHistory.FindSessions(ctx, nil, nil, listing.SandboxUID)
		if err != nil {
			return nil, status.Error(codes.Unavailable, "count archived Environment Threads")
		}
		var cursor *threads.PageCursor
		for {
			countPage, err := s.threadReader.ListPage(ctx, threadMaxPageSize, cursor, true)
			if err != nil {
				return nil, status.Error(codes.Unavailable, "count archived Environment Threads")
			}
			for _, thread := range countPage.Threads {
				if _, belongs := matching[thread.ID]; belongs && thread.Archived {
					response.ArchivedCount++
				}
			}
			if countPage.Next == nil {
				break
			}
			if cursor != nil && !cursorBefore(cursor, countPage.Next) {
				return nil, status.Error(codes.Internal, "Thread metadata page cursor did not advance")
			}
			cursor = countPage.Next
		}
		return response, nil
	}
	after, err := decodeThreadPageToken(token, listing)
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	page, err := s.threadReader.ListPage(ctx, limit, after, listing.IncludeArchived)
	if err != nil {
		return nil, status.Error(codes.Internal, "list Thread metadata")
	}
	archivedCount, err := s.threadReader.ArchivedCount(ctx)
	if err != nil {
		return nil, status.Error(codes.Internal, "count archived Threads")
	}
	response := &apppb.ListThreadMetadataResponse{
		Threads: make([]*apppb.ThreadMetadata, 0, len(page.Threads)), ArchivedCount: int32(archivedCount),
	}
	for _, thread := range page.Threads {
		response.Threads = append(response.Threads, threadMetadataProto(thread))
	}
	if page.Next != nil {
		response.NextPageToken, err = encodeThreadPageToken(listing, page.Next.CreatedAt, page.Next.ID)
		if err != nil {
			return nil, status.Error(codes.Internal, "encode Thread metadata page token")
		}
	}
	return response, nil
}

type threadMetadataWatch struct {
	ctx         context.Context
	updates     <-chan struct{}
	unsubscribe func()
	service     *appService
	initial     bool
}

func (s *appService) newThreadMetadataWatch(ctx context.Context) (*threadMetadataWatch, error) {
	if s.updates == nil {
		return nil, status.Error(codes.Unavailable, "Thread metadata updates are unavailable")
	}
	updates, unsubscribe, err := s.updates.Subscribe(threads.UpdatesChannel)
	if err != nil {
		return nil, status.Error(codes.Internal, "subscribe to Thread metadata updates")
	}
	return &threadMetadataWatch{ctx: ctx, updates: updates, unsubscribe: unsubscribe, service: s, initial: true}, nil
}

func (w *threadMetadataWatch) next() (*apppb.WatchThreadMetadataResponse, error) {
	if !w.initial {
		select {
		case <-w.ctx.Done():
			return nil, status.FromContextError(w.ctx.Err()).Err()
		case <-w.service.drainDone:
			return nil, io.EOF
		case <-w.updates:
		}
	}
	w.initial = false
	connected := w.service.updatesConnected != nil && w.service.updatesConnected()
	return &apppb.WatchThreadMetadataResponse{
		Changed: &emptypb.Empty{}, UpdatesConnected: connected,
	}, nil
}

func threadMetadataProto(thread threads.Thread) *apppb.ThreadMetadata {
	return &apppb.ThreadMetadata{
		ThreadId: thread.ID.String(), CreatedAt: timestamppb.New(thread.CreatedAt), Name: thread.Name, Archived: thread.Archived,
	}
}

func listThreadViews(ctx context.Context, store threadReadStore, history threadReadHistory, listing threadListing, limit int, token *string) (threadPage, error) {
	if limit < 1 || limit > threadMaxPageSize {
		return threadPage{}, fmt.Errorf("page size must be between 1 and %d", threadMaxPageSize)
	}
	after, err := decodeThreadPageToken(token, listing)
	if err != nil {
		return threadPage{}, err
	}
	page := threadPage{Threads: make([]threadView, 0, limit)}
	var sandboxMatches map[uuid.UUID]string
	if listing.Sandbox != nil {
		sandboxMatches, err = history.FindSessions(ctx, listing.Sandbox, nil, nil)
		if err != nil {
			return threadPage{}, err
		}
	}
	var sandboxUIDMatches map[uuid.UUID]string
	if listing.SandboxUID != nil {
		sandboxUIDMatches, err = history.FindSessions(ctx, nil, nil, listing.SandboxUID)
		if err != nil {
			return threadPage{}, err
		}
	}
	var searchSandboxMatches map[uuid.UUID]string
	if listing.Search != nil {
		searchSandboxMatches, err = history.FindSessions(ctx, nil, listing.Search, nil)
		if err != nil {
			return threadPage{}, err
		}
	}
	for {
		stored, err := store.ListPage(ctx, threads.MaxPage, after, listing.IncludeArchived)
		if err != nil {
			return threadPage{}, err
		}
		ids := make([]uuid.UUID, 0, len(stored.Threads))
		for _, thread := range stored.Threads {
			ids = append(ids, thread.ID)
		}
		summaries, err := history.BatchGetSummaries(ctx, ids)
		if err != nil {
			return threadPage{}, err
		}
		for _, thread := range stored.Threads {
			summary, ok := summaries[thread.ID]
			if !ok || !summary.Present || !threadMatches(thread, listing, sandboxMatches, sandboxUIDMatches, searchSandboxMatches) {
				continue
			}
			view, err := makeThreadView(thread, summary)
			if err != nil {
				return threadPage{}, err
			}
			if len(page.Threads) == limit {
				last := page.Threads[len(page.Threads)-1]
				value, err := encodeThreadPageToken(listing, last.CreatedAt, last.ID)
				if err != nil {
					return threadPage{}, err
				}
				page.NextPageToken = &value
				return page, nil
			}
			page.Threads = append(page.Threads, view)
		}
		if stored.Next == nil {
			return page, nil
		}
		if after != nil && !cursorBefore(after, stored.Next) {
			return threadPage{}, errors.New("Thread metadata page cursor did not advance")
		}
		after = stored.Next
	}
}

func threadMatches(
	thread threads.Thread,
	listing threadListing,
	exactSandbox, exactSandboxUID, searchSandbox map[uuid.UUID]string,
) bool {
	if listing.Sandbox != nil {
		if _, ok := exactSandbox[thread.ID]; !ok {
			return false
		}
	}
	if listing.SandboxUID != nil {
		if _, ok := exactSandboxUID[thread.ID]; !ok {
			return false
		}
	}
	if listing.Search == nil {
		return true
	}
	search := strings.ToLower(*listing.Search)
	if thread.Name != nil && strings.Contains(strings.ToLower(*thread.Name), search) {
		return true
	}
	if strings.HasPrefix(strings.ToLower(thread.ID.String()), search) {
		return true
	}
	_, ok := searchSandbox[thread.ID]
	return ok
}

func cursorBefore(left, right *threads.PageCursor) bool {
	return left.CreatedAt.After(right.CreatedAt) || (left.CreatedAt.Equal(right.CreatedAt) && left.ID.String() > right.ID.String())
}

var (
	errMalformedThreadPageToken = errors.New("malformed Thread page token")
	errMismatchedThreadListing  = errors.New("Thread page token was issued for another listing")
)

func decodeThreadPageToken(token *string, listing threadListing) (*threads.PageCursor, error) {
	if token == nil {
		return nil, nil
	}
	data, err := base64.URLEncoding.DecodeString(*token)
	if err != nil {
		data, err = base64.RawURLEncoding.DecodeString(*token)
		if err != nil {
			return nil, errMalformedThreadPageToken
		}
	}
	var value threadPageToken
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return nil, errMalformedThreadPageToken
	}
	for _, field := range []string{"listing", "created_at", "id"} {
		if _, ok := fields[field]; !ok {
			return nil, errMalformedThreadPageToken
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&value); err != nil {
		return nil, errMalformedThreadPageToken
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return nil, errMalformedThreadPageToken
	}
	if !sameThreadListing(value.Listing, listing) {
		return nil, errMismatchedThreadListing
	}
	return &threads.PageCursor{CreatedAt: value.CreatedAt, ID: value.ID}, nil
}

func encodeThreadPageToken(listing threadListing, created time.Time, id uuid.UUID) (string, error) {
	data, err := json.Marshal(threadPageToken{Listing: listing, CreatedAt: created, ID: id})
	if err != nil {
		return "", err
	}
	return base64.URLEncoding.EncodeToString(data), nil
}

func sameThreadListing(left, right threadListing) bool {
	return equalOptionalString(left.Sandbox, right.Sandbox) &&
		equalOptionalString(left.SandboxUID, right.SandboxUID) &&
		equalOptionalString(left.Search, right.Search) && left.IncludeArchived == right.IncludeArchived
}

func equalOptionalString(left, right *string) bool {
	if left == nil || right == nil {
		return left == nil && right == nil
	}
	return *left == *right
}

func readThreadSummary(ctx context.Context, history threadReadHistory, id uuid.UUID) (historydata.FoldSummary, error) {
	return history.GetSummary(ctx, id)
}

func makeThreadView(thread threads.Thread, summary historydata.FoldSummary) (threadView, error) {
	if summary.Sandbox == "" {
		return threadView{}, errors.New("History Session has no Sandbox identity")
	}
	view := threadView{
		ID: thread.ID, Sandbox: summary.Sandbox, CreatedAt: thread.CreatedAt, Name: thread.Name, Archived: thread.Archived,
		LastCursor: summary.FoldedThrough, LastEventAt: summary.LastEventAt,
		LastModelActivityAt: summary.LastModelActivityAt, IdleSince: summary.IdleSince,
		HarnessState: sessionpb.HarnessState_HARNESS_STATE_UNSPECIFIED.String(),
	}
	if summary.SandboxUID != "" {
		view.SandboxUID = stringPointer(summary.SandboxUID)
	}
	if summary.LastTurnStatus != nil {
		value := eventpb.TurnStatus_name[*summary.LastTurnStatus]
		if value == "" {
			return threadView{}, errors.New("History Service returned an unknown TurnStatus")
		}
		view.LastTurnStatus = &value
	}
	if summary.Feed == nil || summary.Feed.Attached == nil {
		return view, nil
	}
	attachment := summary.Feed.Attached
	state := sessionpb.HarnessState_name[int32(attachment.GetHarnessState())]
	setup := sessionpb.SetupState_name[int32(attachment.GetSetupState())]
	outcome := sessionpb.LaunchOutcome_name[int32(attachment.GetLaunchOutcome())]
	if state == "" || setup == "" || outcome == "" {
		return threadView{}, errors.New("History Service returned an unknown Session lifecycle state")
	}
	view.HarnessState = state
	view.SetupState, view.LaunchOutcome = &setup, &outcome
	if attachment.GetActiveTurnId() != "" {
		view.ActiveTurnID = stringPointer(attachment.GetActiveTurnId())
	}
	if spec := attachment.GetSpec(); spec != nil {
		harness := sessionpb.Harness_name[int32(spec.GetHarness())]
		if harness == "" || harness == "HARNESS_UNSPECIFIED" {
			return threadView{}, errors.New("History Service returned an unknown Harness")
		}
		view.Harness, view.Model, view.CWD = &harness, stringPointer(spec.GetModel()), stringPointer(spec.GetCwd())
		view.ReasoningEffort = stringPointer(spec.GetReasoningEffort())
	}
	switch summary.Feed.Status {
	case historypb.FeedStatus_FEED_STATUS_ACTIVE:
		view.FeedStatus = stringPointer("active")
	case historypb.FeedStatus_FEED_STATUS_ENDED:
		view.FeedStatus = stringPointer("ended")
	case historypb.FeedStatus_FEED_STATUS_FAILED:
		view.FeedStatus = stringPointer("failed")
	default:
		return threadView{}, errors.New("History Service returned an unknown feed status")
	}
	return view, nil
}

func stringPointer(value string) *string { return &value }

func ensureThreadArchivable(ctx context.Context, namespace string, thread threads.Thread, summary historydata.FoldSummary, inventory threadSandboxInventory, environmentRuntime threadEnvironmentRuntimeStatusReader) error {
	if inventory == nil {
		return errors.New("Sandbox inventory is unavailable")
	}
	if summary.SandboxUID == "" {
		return errors.New("Thread has no Sandbox UID; its Environment cannot be identified")
	}
	sandbox, err := inventory.GetExact(ctx, &environmentpb.TargetHandle{Namespace: namespace, Name: summary.Sandbox, Uid: summary.SandboxUID})
	if status.Code(err) == codes.NotFound {
		return nil // This exact Environment is gone, so it cannot have a running harness.
	}
	if err != nil {
		return err
	}
	if sandbox.GetPod() == nil {
		return nil
	}
	if !threadSandboxReady(sandbox) {
		return errThreadEnvironmentRuntimeUnready
	}
	if environmentRuntime == nil {
		return errors.New("Environment Runtime status is unavailable")
	}
	status, err := environmentRuntime.EnvironmentRuntimeStatus(ctx, sandbox)
	if err != nil {
		return err
	}
	for _, session := range status.GetSessions() {
		if session.GetSessionId() == thread.ID.String() && session.GetBusy() {
			return errThreadBusy
		}
	}
	return nil
}

var (
	errThreadEnvironmentRuntimeUnready = errors.New("cannot verify the Environment Runtime while a Pod exists; wait for its status to become ready or for it to be removed")
	errThreadBusy                      = errors.New("finish or interrupt active work before archiving this thread")
)

func threadSandboxReady(sandbox *environmentpb.EnvironmentDescriptor) bool {
	return sandbox != nil && sandbox.GetOperatingMode() == environmentpb.OperatingMode_OPERATING_MODE_RUNNING &&
		sandbox.GetReadiness().GetState() == environmentpb.ReadinessState_READINESS_STATE_READY
}

func threadReadHTTPError(err error) error {
	return httperror.LogServerError(classifyThreadReadError(err), err)
}

func classifyThreadReadError(err error) error {
	switch {
	case errors.Is(err, historydata.ErrScopeChanged):
		return huma.Error410Gone("the thread fold projection epoch has changed")
	case errors.Is(err, historydata.ErrEvidenceNotFound):
		return huma.Error404NotFound("no selected thread entity")
	case errors.Is(err, threads.ErrNotFound):
		return huma.Error404NotFound(err.Error())
	case errors.Is(err, errMalformedThreadPageToken), errors.Is(err, errMismatchedThreadListing):
		return huma.Error400BadRequest(err.Error())
	case strings.HasPrefix(err.Error(), "no thread "):
		return huma.Error404NotFound(err.Error())
	case errors.Is(err, errThreadEnvironmentRuntimeUnready), errors.Is(err, errThreadBusy):
		return huma.Error409Conflict(err.Error())
	case errors.Is(err, historydata.ErrInvalidPage), errors.Is(err, historydata.ErrInvalidSessionID):
		return huma.Error503ServiceUnavailable("Upstream service unavailable")
	case errors.Is(err, context.DeadlineExceeded):
		return huma.Error504GatewayTimeout("Upstream deadline expired")
	case isHistoryConnectionError(err):
		return huma.Error503ServiceUnavailable("Upstream service unavailable")
	default:
		switch status.Code(err) {
		case codes.NotFound:
			return huma.Error404NotFound(err.Error())
		case codes.FailedPrecondition, codes.AlreadyExists, codes.Aborted:
			return huma.Error409Conflict(err.Error())
		case codes.InvalidArgument:
			return huma.Error422UnprocessableEntity(err.Error())
		case codes.ResourceExhausted:
			return huma.Error429TooManyRequests(err.Error())
		case codes.DeadlineExceeded:
			return huma.Error504GatewayTimeout("Upstream deadline expired")
		}
		return huma.Error500InternalServerError("Thread metadata could not be read")
	}
}

func isHistoryConnectionError(err error) bool {
	var networkError interface{ Timeout() bool }
	return errors.As(err, &networkError) || errors.Is(err, context.Canceled) || strings.Contains(err.Error(), "History Service") || strings.Contains(err.Error(), "service observation page")
}

func threadArchiveHTTPError(err error) error {
	if errors.Is(err, errThreadBusy) || errors.Is(err, errThreadEnvironmentRuntimeUnready) {
		return huma.Error409Conflict(err.Error())
	}
	return sandboxHTTPError(err)
}

var _ threadReadStore = (*threads.Store)(nil)
