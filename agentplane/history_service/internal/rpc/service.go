// Package rpc serves the History Service's HistoryService gRPC API: the reads of retained Session history and
// of its folded Threads, raw entry follows and summary change watches.
//
// Methods admits a live workload SVID subject. An Identity reads only Sessions retained as executing
// as that Identity incarnation: a public Session UUID is not itself authority to read a transcript.
package rpc

import (
	"context"
	"errors"
	"log/slog"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/reads"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Service is the HistoryService server.
type Service struct {
	historypb.UnimplementedHistoryServiceServer
	reader         *reads.Reader
	wakeups        *wakeups.Hub
	requestTimeout time.Duration
	lease          time.Duration
	draining       context.Context
	drain          context.CancelFunc
}

// Options are what a Service reads.
type Options struct {
	Reader *reads.Reader
	// Wakeups listens to raw history, summary changes and Sessions.
	Wakeups *wakeups.Hub
	// RequestTimeout bounds a call's backend work and each write to a watch or follow stream.
	RequestTimeout time.Duration
	// WatchLease ends each watch and follow after this long: a stream is admitted once, so the lease bounds
	// how long a revoked caller credential keeps reading.
	WatchLease time.Duration
}

// Methods admits workload SVID subjects on every ReadMethods RPC; exact service peers require explicit service
// caller rules.
func Methods(subjects auth.SubjectResolver) auth.Methods {
	rule := auth.AnySubject(subjects)
	methods := auth.Methods{}
	for _, method := range ReadMethods() {
		methods[method] = rule
	}
	return methods
}

// ReadMethods returns the exact read surface whose service callers and delegators may be configured.
func ReadMethods() []string {
	return []string{
		historypb.HistoryService_ReadSessionEvents_FullMethodName,
		historypb.HistoryService_FollowSession_FullMethodName,
		historypb.HistoryService_ReadSessionObservations_FullMethodName,
		historypb.HistoryService_GetSessionSummary_FullMethodName,
		historypb.HistoryService_BatchGetSessionSummaries_FullMethodName,
		historypb.HistoryService_ReadThreadEntities_FullMethodName,
		historypb.HistoryService_ReadEntityEvidence_FullMethodName,
		historypb.HistoryService_ReadNativeFrames_FullMethodName,
		historypb.HistoryService_FindSessions_FullMethodName,
		historypb.HistoryService_WatchSessionSummaries_FullMethodName,
	}
}

func NewService(options Options) *Service {
	draining, drain := context.WithCancel(context.Background())
	return &Service{
		reader: options.Reader, wakeups: options.Wakeups,
		requestTimeout: options.RequestTimeout, lease: options.WatchLease, draining: draining, drain: drain,
	}
}

// Drain ends every open watch and follow, so a shutdown that waits for open calls is not held up until their leases.
func (s *Service) Drain() { s.drain() }

func (s *Service) scopedReader(ctx context.Context) (*reads.Reader, error) {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return nil, err
	}
	// An Identity reads the Sessions recorded with its exact incarnation, whether its own workload
	// called or a trusted platform component acts for it.
	switch identity := admission.(type) {
	case auth.IdentityAdmission:
		return s.reader.WithReadScope(storage.OwnedByIdentity(storage.Identity{
			Name: identity.Caller.Identity.Name, UID: identity.Caller.Identity.UID,
		})), nil
	case auth.ServiceAdmission:
		// Exact service reader allowlists grant the configured backend-wide read role.
		return s.reader.WithReadScope(storage.AllSessions()), nil
	case auth.DelegatedServiceIdentityAdmission:
		return s.reader.WithReadScope(storage.OwnedByIdentity(storage.Identity{
			Name: identity.EffectiveCaller.Name, UID: identity.EffectiveCaller.UID,
		})), nil
	}
	// A workload credential without a retained execution Identity has no History read scope.
	return s.reader.WithReadScope(storage.ReadScope{}), nil
}

// serve runs read, a method of the caller's scoped Reader, under the request timeout.
func serve[Request, Response any](ctx context.Context, s *Service, request *Request, read func(*reads.Reader, context.Context, *Request) (*Response, error)) (*Response, error) {
	reader, err := s.scopedReader(ctx)
	if err != nil {
		return nil, failure(ctx, err)
	}
	ctx, cancel := context.WithTimeout(ctx, s.requestTimeout)
	defer cancel()
	response, err := read(reader, ctx, request)
	if err != nil {
		return nil, failure(ctx, err)
	}
	return response, nil
}

func (s *Service) ReadSessionEvents(ctx context.Context, request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
	return serve(ctx, s, request, (*reads.Reader).ReadSessionEvents)
}

func (s *Service) ReadSessionObservations(ctx context.Context, request *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error) {
	return serve(ctx, s, request, (*reads.Reader).ReadSessionObservations)
}

func (s *Service) GetSessionSummary(ctx context.Context, request *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	return serve(ctx, s, request, (*reads.Reader).GetSessionSummary)
}

func (s *Service) BatchGetSessionSummaries(ctx context.Context, request *historypb.BatchGetSessionSummariesRequest) (*historypb.BatchGetSessionSummariesResponse, error) {
	return serve(ctx, s, request, (*reads.Reader).BatchGetSessionSummaries)
}

func (s *Service) FindSessions(ctx context.Context, request *historypb.FindSessionsRequest) (*historypb.FindSessionsResponse, error) {
	return serve(ctx, s, request, (*reads.Reader).FindSessions)
}

func (s *Service) ReadThreadEntities(ctx context.Context, request *historypb.ReadThreadEntitiesRequest) (*historypb.ReadThreadEntitiesResponse, error) {
	return serve(ctx, s, request, (*reads.Reader).ReadThreadEntities)
}

func (s *Service) ReadEntityEvidence(ctx context.Context, request *historypb.ReadEntityEvidenceRequest) (*historypb.ReadEntityEvidenceResponse, error) {
	return serve(ctx, s, request, (*reads.Reader).ReadEntityEvidence)
}

func (s *Service) ReadNativeFrames(ctx context.Context, request *historypb.ReadNativeFramesRequest) (*historypb.ReadNativeFramesResponse, error) {
	return serve(ctx, s, request, (*reads.Reader).ReadNativeFrames)
}

// failure answers a failed call. The client gets a fixed message per kind of failure: no SQL, bearer or
// Kubernetes response text reaches it. The cause is logged where the message names none.
func failure(ctx context.Context, err error) error {
	switch {
	case status.Code(err) != codes.Unknown:
		// A failure already answered.
		return err
	case errors.Is(err, storage.ErrNotFound):
		return status.Error(codes.NotFound, "session history not found")
	case errors.Is(err, reads.ErrEvidenceNotFound):
		return status.Error(codes.NotFound, "entity evidence not found")
	case errors.Is(err, reads.ErrScopeReset):
		return status.Error(codes.FailedPrecondition, "thread projection epoch was reset")
	case errors.Is(err, reads.ErrInvalidStoredEntity):
		// A stored row outside the published view contract is a server fault, not a bad request.
		slog.Error("stored thread entity failed validation", "error", err)
		return status.Error(codes.Internal, "stored thread entity is invalid")
	case errors.Is(err, storage.ErrInvalidArgument):
		return status.Error(codes.InvalidArgument, "invalid session history request")
	case errors.Is(err, storage.ErrOutOfRange):
		return status.Error(codes.OutOfRange, "cursor beyond committed session history")
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, "history read deadline expired")
	case ctx.Err() != nil:
		return status.FromContextError(ctx.Err()).Err()
	default:
		slog.Warn("history read failed", "error", err)
		return status.Error(codes.Unavailable, "history or authentication unavailable")
	}
}
