// Package threads serves App-owned titles and archived state. Browsers join Session inventory
// and History summaries through the owning services' HTTP facades.
package threads

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	threadstore "github.com/agentydragon/ducktape/agentplane/app/internal/threads"

	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
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

// Store is the app-owned Thread metadata store.
type Store interface {
	NoticeStore
	GetThread(context.Context, uuid.UUID) (*threadstore.Thread, error)
	ListPage(context.Context, int, *threadstore.PageCursor, bool) (threadstore.Page, error)
	ArchivedCount(context.Context) (int, error)
	Rename(context.Context, uuid.UUID, *string) (threadstore.Thread, error)
	Archive(context.Context, uuid.UUID) (threadstore.Thread, error)
	Unarchive(context.Context, uuid.UUID) (threadstore.Thread, error)
}

// SessionReader reads authoritative current activity for the archive mutation policy.
type SessionReader interface {
	GetSession(context.Context, *dispatcherpb.GetSessionRequest) (*dispatcherpb.GetSessionResponse, error)
}

type threadArchiveBusyError struct {
	reasons []string
}

func (e threadArchiveBusyError) Error() string {
	return strings.Join(e.reasons, " ") + " Wait for the reported work to finish before archiving this Thread."
}

func (e threadArchiveBusyError) Is(target error) bool { return target == errThreadBusy }

// Updates subscribes to app-owned database change notifications.
type Updates interface {
	Subscribe(channels ...string) (<-chan struct{}, func(), error)
}

// Dependencies are the concrete inputs needed to serve app-owned Thread metadata.
type Dependencies struct {
	Store            Store
	Dispatcher       SessionReader
	Updates          Updates
	UpdatesConnected func() bool
	DrainDone        <-chan struct{}
}

// Handler implements the app's Thread metadata RPCs and its typed watch stream.
type Handler struct {
	// The generated in-process gateway accepts the full aggregate service interface. This owner
	// intentionally implements only Thread methods; all other RPCs stay unimplemented here and are
	// not mounted by RegisterRoutes.
	apppb.UnimplementedAppServiceServer
	store            Store
	dispatcher       SessionReader
	updates          Updates
	updatesConnected func() bool
	drainDone        <-chan struct{}
}

// NewHandler creates a Thread metadata handler from its owning stores and readers.
func NewHandler(dependencies Dependencies) *Handler {
	return &Handler{
		store: dependencies.Store, dispatcher: dependencies.Dispatcher,
		updates: dependencies.Updates, updatesConnected: dependencies.UpdatesConnected,
		drainDone: dependencies.DrainDone,
	}
}

type threadListing struct {
	IncludeArchived bool `json:"include_archived"`
}

type threadPageToken struct {
	Listing   threadListing `json:"listing"`
	CreatedAt time.Time     `json:"created_at"`
	ID        uuid.UUID     `json:"id"`
}

// WatchHandler streams Thread metadata updates with the generated gateway's ProtoJSON encoder.
func (h *Handler) WatchHandler(mux *gatewayruntime.ServeMux) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		ctx := gatewayruntime.NewServerMetadataContext(request.Context(), gatewayruntime.ServerMetadata{})
		_, marshaler := gatewayruntime.MarshalerForRequest(mux, request)
		watch, err := h.newThreadMetadataWatch(ctx)
		if watch != nil {
			defer watch.unsubscribe()
		}
		gatewayruntime.ForwardResponseStream(ctx, mux, marshaler, w, request, func() (proto.Message, error) {
			if err != nil {
				return nil, err
			}
			return watch.next()
		})
	})
}

func threadTargetID(value string) (uuid.UUID, error) {
	id, err := uuid.Parse(value)
	if err != nil || id.String() != value {
		return uuid.Nil, status.Error(codes.InvalidArgument, "thread_id must be a canonical UUID")
	}
	return id, nil
}

func (h *Handler) RenameThread(ctx context.Context, request *apppb.RenameThreadRequest) (*apppb.ThreadMetadata, error) {
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
	thread, err := h.store.Rename(ctx, id, name)
	if err != nil {
		return nil, threadMutationRPCError(err)
	}
	return threadMetadataProto(thread), nil
}

func (h *Handler) ArchiveThread(ctx context.Context, request *apppb.ThreadMetadataTarget) (*emptypb.Empty, error) {
	id, err := threadTargetID(request.GetThreadId())
	if err != nil {
		return nil, err
	}
	if h.dispatcher == nil {
		return nil, status.Error(codes.Unavailable, "Session activity is unavailable")
	}
	response, err := h.dispatcher.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: id.String()})
	if err != nil {
		return nil, threadMutationRPCError(err)
	}
	if response.GetSession().GetSessionId() != id.String() {
		return nil, status.Error(codes.Unavailable, "Session activity response did not match the requested Session")
	}
	if err := ensureThreadArchivable(id.String(), response.GetSession().GetStatus().GetActivity()); err != nil {
		return nil, threadMutationRPCError(err)
	}
	if _, err := h.store.Archive(ctx, id); err != nil {
		return nil, threadMutationRPCError(err)
	}
	return &emptypb.Empty{}, nil
}

func (h *Handler) UnarchiveThread(ctx context.Context, request *apppb.ThreadMetadataTarget) (*emptypb.Empty, error) {
	id, err := threadTargetID(request.GetThreadId())
	if err != nil {
		return nil, err
	}
	if _, err := h.store.Unarchive(ctx, id); err != nil {
		return nil, threadMutationRPCError(err)
	}
	return &emptypb.Empty{}, nil
}

func (h *Handler) GetThreadMetadata(ctx context.Context, request *apppb.ThreadMetadataTarget) (*apppb.ThreadMetadata, error) {
	id, err := threadTargetID(request.GetThreadId())
	if err != nil {
		return nil, err
	}
	thread, err := h.store.GetThread(ctx, id)
	if err != nil {
		return nil, status.Error(codes.Internal, "read Thread metadata")
	}
	if thread == nil {
		return nil, status.Error(codes.NotFound, "Thread metadata not found")
	}
	return threadMetadataProto(*thread), nil
}

func (h *Handler) ListThreadMetadata(ctx context.Context, request *apppb.ListThreadMetadataRequest) (*apppb.ListThreadMetadataResponse, error) {
	limit := int(request.GetLimit())
	if limit == 0 {
		limit = 50
	}
	if limit < 1 || limit > threadMaxPageSize {
		return nil, status.Errorf(codes.InvalidArgument, "limit must be between 1 and %d", threadMaxPageSize)
	}
	listing := threadListing{IncludeArchived: request.GetIncludeArchived()}
	var token *string
	if request.GetPageToken() != "" {
		value := request.GetPageToken()
		token = &value
	}
	after, err := decodeThreadPageToken(token, listing)
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	page, err := h.store.ListPage(ctx, limit, after, listing.IncludeArchived)
	if err != nil {
		return nil, status.Error(codes.Internal, "list Thread metadata")
	}
	archivedCount, err := h.store.ArchivedCount(ctx)
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
	handler     *Handler
	initial     bool
}

func (h *Handler) newThreadMetadataWatch(ctx context.Context) (*threadMetadataWatch, error) {
	if h.updates == nil {
		return nil, status.Error(codes.Unavailable, "Thread metadata updates are unavailable")
	}
	updates, unsubscribe, err := h.updates.Subscribe(threadstore.UpdatesChannel)
	if err != nil {
		return nil, status.Error(codes.Internal, "subscribe to Thread metadata updates")
	}
	return &threadMetadataWatch{ctx: ctx, updates: updates, unsubscribe: unsubscribe, handler: h, initial: true}, nil
}

func (w *threadMetadataWatch) next() (*apppb.WatchThreadMetadataResponse, error) {
	if !w.initial {
		select {
		case <-w.ctx.Done():
			return nil, status.FromContextError(w.ctx.Err()).Err()
		case <-w.handler.drainDone:
			return nil, io.EOF
		case <-w.updates:
		}
	}
	w.initial = false
	connected := w.handler.updatesConnected != nil && w.handler.updatesConnected()
	return &apppb.WatchThreadMetadataResponse{
		Changed: &emptypb.Empty{}, UpdatesConnected: connected,
	}, nil
}

func threadMetadataProto(thread threadstore.Thread) *apppb.ThreadMetadata {
	return &apppb.ThreadMetadata{
		ThreadId: thread.ID.String(), CreatedAt: timestamppb.New(thread.CreatedAt), Name: thread.Name, Archived: thread.Archived,
	}
}

var (
	errMalformedThreadPageToken = errors.New("malformed Thread page token")
	errMismatchedThreadListing  = errors.New("Thread page token was issued for another listing")
)

func decodeThreadPageToken(token *string, listing threadListing) (*threadstore.PageCursor, error) {
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
	if value.Listing != listing {
		return nil, errMismatchedThreadListing
	}
	return &threadstore.PageCursor{CreatedAt: value.CreatedAt, ID: value.ID}, nil
}

func encodeThreadPageToken(listing threadListing, created time.Time, id uuid.UUID) (string, error) {
	data, err := json.Marshal(threadPageToken{Listing: listing, CreatedAt: created, ID: id})
	if err != nil {
		return "", err
	}
	return base64.URLEncoding.EncodeToString(data), nil
}

func ensureThreadArchivable(sessionID string, activity *dispatcherpb.SessionActivity) error {
	var observed *harnesshostpb.Session
	switch kind := activity.GetKind().(type) {
	case *dispatcherpb.SessionActivity_Observed:
		observed = kind.Observed
	case *dispatcherpb.SessionActivity_EnvironmentDown_:
		switch kind.EnvironmentDown.GetCause() {
		case dispatcherpb.SessionActivity_EnvironmentDown_SUSPENDED, dispatcherpb.SessionActivity_EnvironmentDown_DELETED:
			return nil
		}
		return errThreadActivityUnknown
	default:
		return errThreadActivityUnknown
	}
	if observed == nil || observed.GetSessionId() != sessionID {
		return errThreadActivityUnknown
	}
	var setup sessionpb.SetupState
	preparing := false
	switch host := observed.GetHost().(type) {
	case *harnesshostpb.Session_EnvironmentHosted:
		setup = host.EnvironmentHosted.GetSetup()
		switch host.EnvironmentHosted.GetHarness().GetState() {
		case harnesshostpb.HarnessAttachment_STATE_DETACHED, harnesshostpb.HarnessAttachment_STATE_ATTACHED:
		case harnesshostpb.HarnessAttachment_STATE_STARTING:
			preparing = true
		default:
			return errThreadActivityUnknown
		}
	case *harnesshostpb.Session_CentralSamplerV1:
		setup = host.CentralSamplerV1.GetSetup()
	default:
		return errThreadActivityUnknown
	}
	switch setup {
	case sessionpb.SetupState_SETUP_STATE_NOT_REQUIRED, sessionpb.SetupState_SETUP_STATE_SUCCEEDED,
		sessionpb.SetupState_SETUP_STATE_FAILED, sessionpb.SetupState_SETUP_STATE_INTERRUPTED,
		sessionpb.SetupState_SETUP_STATE_RUNNING:
	default:
		return errThreadActivityUnknown
	}
	var reasons []string
	if observed.GetActiveTurnId() != "" {
		reasons = append(reasons, "An active turn is still running.")
	}
	if observed.GetPendingCommands() > 0 {
		reasons = append(reasons, "A command is still pending.")
	}
	if setup == sessionpb.SetupState_SETUP_STATE_RUNNING {
		reasons = append(reasons, "Setup is still running.")
	}
	if preparing {
		reasons = append(reasons, "The Session is still preparing.")
	}
	if len(reasons) > 0 {
		return threadArchiveBusyError{reasons: reasons}
	}
	return nil
}

var (
	errThreadActivityUnknown = errors.New("Session activity is unknown; wait for current activity before archiving this Thread")
	errThreadBusy            = errors.New("thread has unfinished work")
)

func threadMutationRPCError(err error) error {
	switch {
	case errors.Is(err, errThreadActivityUnknown), errors.Is(err, errThreadBusy):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, "Upstream deadline expired")
	default:
		switch status.Code(err) {
		case codes.NotFound, codes.FailedPrecondition, codes.AlreadyExists, codes.Aborted, codes.InvalidArgument, codes.ResourceExhausted, codes.DeadlineExceeded, codes.Unavailable:
			return err
		}
		slog.Error("Thread metadata mutation failed", "error", err)
		return status.Error(codes.Internal, "Thread metadata could not be updated")
	}
}

var _ Store = (*threadstore.Store)(nil)
