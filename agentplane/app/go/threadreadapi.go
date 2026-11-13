package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/agentydragon/ducktape/agentplane/app/go/historydata"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	gatewayruntime "github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
	"k8s.io/utils/ptr"
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
	GetSummary(context.Context, uuid.UUID) (*historypb.SessionSummary, error)
	BatchGetSummaries(context.Context, []uuid.UUID) (map[uuid.UUID]*historypb.SessionSummary, error)
	FindSessions(context.Context, *string, *string, *string) (map[uuid.UUID]string, error)
}

type threadEnvironmentRuntimeStatusReader interface {
	EnvironmentRuntimeStatus(context.Context, *environmentpb.EnvironmentDescriptor) (*sessionpb.EnvironmentRuntimeStatus, error)
}

type threadListing struct {
	SandboxUID      *string `json:"sandbox_uid"`
	IncludeArchived bool    `json:"include_archived"`
}

type threadPageToken struct {
	Listing   threadListing `json:"listing"`
	CreatedAt time.Time     `json:"created_at"`
	ID        uuid.UUID     `json:"id"`
}

func registerThreadMetadataGateway(server *HTTPServer, service *appService) error {
	mux := gatewayruntime.NewServeMux(gatewayruntime.WithErrorHandler(sessionGatewayError))
	if err := apppb.RegisterAppServiceHandlerServer(context.Background(), mux, service); err != nil {
		return fmt.Errorf("register generated Thread Metadata gateway: %w", err)
	}
	gateway := http.StripPrefix("/upstream/app", mux)
	server.RegisterRawRoute("GET /upstream/app/v1/threads", gateway, false)
	server.RegisterRawRoute("GET /upstream/app/v1/threads/{thread_id}", gateway, false)
	server.RegisterRawRoute("PATCH /upstream/app/v1/threads/{thread_id}", gateway, false)
	server.RegisterRawRoute("POST /upstream/app/v1/threads/{thread_id}/archive", gateway, false)
	server.RegisterRawRoute("POST /upstream/app/v1/threads/{thread_id}/unarchive", gateway, false)
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

func threadTargetID(value string) (uuid.UUID, error) {
	id, err := uuid.Parse(value)
	if err != nil || id.String() != value {
		return uuid.Nil, status.Error(codes.InvalidArgument, "thread_id must be a canonical UUID")
	}
	return id, nil
}

func (s *appService) RenameThread(ctx context.Context, request *apppb.RenameThreadRequest) (*apppb.ThreadMetadata, error) {
	id, err := threadTargetID(request.GetThreadId())
	if err != nil {
		return nil, err
	}
	name := request.Name
	if name != nil {
		trimmed := strings.TrimSpace(*name)
		if !utf8.ValidString(trimmed) || utf8.RuneCountInString(trimmed) > 200 {
			return nil, status.Error(codes.InvalidArgument, "Thread name must be at most 200 characters")
		}
		if trimmed == "" {
			name = nil
		} else {
			name = &trimmed
		}
	}
	summary, err := s.threadHistory.GetSummary(ctx, id)
	if err != nil {
		return nil, threadMutationRPCError(err)
	}
	if summary == nil {
		return nil, status.Errorf(codes.NotFound, "no thread %s", id)
	}
	thread, err := s.threadReader.Rename(ctx, id, name)
	if err != nil {
		return nil, threadMutationRPCError(err)
	}
	return threadMetadataProto(thread), nil
}

func (s *appService) ArchiveThread(ctx context.Context, request *apppb.ThreadMetadataTarget) (*emptypb.Empty, error) {
	id, err := threadTargetID(request.GetThreadId())
	if err != nil {
		return nil, err
	}
	thread, err := s.threadReader.GetThread(ctx, id)
	if err != nil {
		return nil, threadMutationRPCError(err)
	}
	if thread == nil {
		return nil, status.Errorf(codes.NotFound, "no thread %s", id)
	}
	summary, err := s.threadHistory.GetSummary(ctx, id)
	if err != nil {
		return nil, threadMutationRPCError(err)
	}
	if summary == nil {
		return nil, status.Errorf(codes.NotFound, "no thread %s", id)
	}
	if err := ensureThreadArchivable(ctx, s.config.SandboxNamespace, *thread, summary, s.environments, s.threadRuntime); err != nil {
		return nil, threadMutationRPCError(err)
	}
	if _, err := s.threadReader.Archive(ctx, id); err != nil {
		return nil, threadMutationRPCError(err)
	}
	return &emptypb.Empty{}, nil
}

func (s *appService) UnarchiveThread(ctx context.Context, request *apppb.ThreadMetadataTarget) (*emptypb.Empty, error) {
	id, err := threadTargetID(request.GetThreadId())
	if err != nil {
		return nil, err
	}
	if _, err := s.threadReader.Unarchive(ctx, id); err != nil {
		return nil, threadMutationRPCError(err)
	}
	return &emptypb.Empty{}, nil
}

func (s *appService) GetThreadMetadata(ctx context.Context, request *apppb.ThreadMetadataTarget) (*apppb.ThreadMetadata, error) {
	id, err := threadTargetID(request.GetThreadId())
	if err != nil {
		return nil, err
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
		page, err := listEnvironmentThreads(ctx, s.threadReader, s.threadHistory, listing, limit, token)
		if err != nil {
			if errors.Is(err, errMalformedThreadPageToken) || errors.Is(err, errMismatchedThreadListing) {
				return nil, status.Error(codes.InvalidArgument, err.Error())
			}
			return nil, status.Error(codes.Unavailable, "list Environment Threads")
		}
		response := &apppb.ListThreadMetadataResponse{Threads: make([]*apppb.ThreadMetadata, 0, len(page.Threads))}
		for _, thread := range page.Threads {
			response.Threads = append(response.Threads, threadMetadataProto(thread))
		}
		if page.Next != nil {
			response.NextPageToken, err = encodeThreadPageToken(listing, page.Next.CreatedAt, page.Next.ID)
			if err != nil {
				return nil, status.Error(codes.Unavailable, "list Environment Threads")
			}
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
			ids := make([]uuid.UUID, 0, len(countPage.Threads))
			for _, row := range countPage.Threads {
				if _, belongs := matching[row.ID]; belongs && row.Archived {
					ids = append(ids, row.ID)
				}
			}
			summaries, err := s.threadHistory.BatchGetSummaries(ctx, ids)
			if err != nil {
				return nil, status.Error(codes.Unavailable, "count archived Environment Threads")
			}
			for _, id := range ids {
				if summary := summaries[id]; summary != nil && summary.GetSandbox().GetUid() == *listing.SandboxUID {
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

func listEnvironmentThreads(ctx context.Context, store threadReadStore, history threadReadHistory, listing threadListing, limit int, token *string) (threads.Page, error) {
	if limit < 1 || limit > threadMaxPageSize {
		return threads.Page{}, fmt.Errorf("page size must be between 1 and %d", threadMaxPageSize)
	}
	after, err := decodeThreadPageToken(token, listing)
	if err != nil {
		return threads.Page{}, err
	}
	if listing.SandboxUID == nil {
		return threads.Page{}, errors.New("Environment Thread listing requires a Sandbox UID")
	}
	sandboxUIDMatches, err := history.FindSessions(ctx, nil, nil, listing.SandboxUID)
	if err != nil {
		return threads.Page{}, err
	}
	page := threads.Page{Threads: make([]threads.Thread, 0, limit)}
	for {
		stored, err := store.ListPage(ctx, threads.MaxPage, after, listing.IncludeArchived)
		if err != nil {
			return threads.Page{}, err
		}
		ids := make([]uuid.UUID, 0, len(stored.Threads))
		for _, thread := range stored.Threads {
			ids = append(ids, thread.ID)
		}
		summaries, err := history.BatchGetSummaries(ctx, ids)
		if err != nil {
			return threads.Page{}, err
		}
		for _, thread := range stored.Threads {
			summary := summaries[thread.ID]
			_, matchesSandboxUID := sandboxUIDMatches[thread.ID]
			if summary == nil || !matchesSandboxUID || summary.GetSandbox().GetUid() != *listing.SandboxUID {
				continue
			}
			if len(page.Threads) == limit {
				last := page.Threads[len(page.Threads)-1]
				page.Next = &threads.PageCursor{CreatedAt: last.CreatedAt, ID: last.ID}
				return page, nil
			}
			page.Threads = append(page.Threads, thread)
		}
		if stored.Next == nil {
			return page, nil
		}
		if after != nil && !cursorBefore(after, stored.Next) {
			return threads.Page{}, errors.New("Thread metadata page cursor did not advance")
		}
		after = stored.Next
	}
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
	return ptr.Equal(left.SandboxUID, right.SandboxUID) && left.IncludeArchived == right.IncludeArchived
}

func stringPointer(value string) *string { return &value }

func ensureThreadArchivable(ctx context.Context, namespace string, thread threads.Thread, summary *historypb.SessionSummary, inventory sessionEnvironmentReader, environmentRuntime threadEnvironmentRuntimeStatusReader) error {
	sandboxRef := summary.GetSandbox()
	if sandboxRef.GetName() == "" {
		return errors.New("Thread has no Sandbox identity; its Environment cannot be identified")
	}
	if inventory == nil {
		return errors.New("Sandbox inventory is unavailable")
	}
	if sandboxRef.GetUid() == "" {
		return errors.New("Thread has no Sandbox UID; its Environment cannot be identified")
	}
	sandbox, err := inventory.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: &identitypb.EnvironmentTarget{Namespace: namespace, Name: sandboxRef.GetName(), Uid: sandboxRef.GetUid()}})
	if status.Code(err) == codes.NotFound {
		return nil // This exact Environment is gone, so it cannot have a running harness.
	}
	if err != nil {
		return err
	}
	if sandbox.GetKubernetesDiagnostics().GetPod() == nil {
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
	return sandbox != nil && sandbox.GetObservedState().GetOperatingMode() == environmentpb.OperatingMode_OPERATING_MODE_RUNNING &&
		sandbox.GetObservedState().GetReadiness().GetState() == environmentpb.ReadinessState_READINESS_STATE_READY
}

func threadMutationRPCError(err error) error {
	switch {
	case errors.Is(err, threads.ErrNotFound):
		return status.Error(codes.NotFound, err.Error())
	case errors.Is(err, errThreadEnvironmentRuntimeUnready), errors.Is(err, errThreadBusy):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, "Upstream deadline expired")
	case errors.Is(err, historydata.ErrInvalidPage), errors.Is(err, historydata.ErrInvalidSessionID), isHistoryConnectionError(err):
		return status.Error(codes.Unavailable, "Upstream service unavailable")
	default:
		switch status.Code(err) {
		case codes.NotFound, codes.FailedPrecondition, codes.AlreadyExists, codes.Aborted, codes.InvalidArgument, codes.ResourceExhausted, codes.DeadlineExceeded, codes.Unavailable:
			return err
		}
		slog.Error("Thread metadata mutation failed", "error", err)
		return status.Error(codes.Internal, "Thread metadata could not be updated")
	}
}

func isHistoryConnectionError(err error) bool {
	var networkError interface{ Timeout() bool }
	return errors.As(err, &networkError) || errors.Is(err, context.Canceled) || strings.Contains(err.Error(), "History Service") || strings.Contains(err.Error(), "service observation page")
}

var _ threadReadStore = (*threads.Store)(nil)
