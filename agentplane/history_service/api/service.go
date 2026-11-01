// Package api serves the History Service's HistoryService gRPC API: the reads of retained Session history and
// of its folded Threads, raw entry follows and summary change watches.
//
// Methods admits only the configured callers, each proving a ServiceAccount by a bearer reviewed on that call
// (agentplane/docs/grpc_services.md): a public Session UUID is not itself authority to read a transcript.
package api

import (
	"context"
	"errors"
	"log/slog"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/reads"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Service is the HistoryService server.
type Service struct {
	historypb.UnimplementedHistoryServiceServer
	reader         *reads.Reader
	readerAccounts map[auth.Account]struct{}
	wakeups        *wakeups.Hub
	requestTimeout time.Duration
	lease          time.Duration
	draining       context.Context
	drain          context.CancelFunc
}

// Options are what a Service reads.
type Options struct {
	Reader *reads.Reader
	// ReaderAccounts retain the existing Pod-bound backend-wide read path.
	ReaderAccounts []auth.Account
	// Wakeups listens to raw history, summary changes and Session openings.
	Wakeups *wakeups.Hub
	// RequestTimeout bounds a call's backend work and each write to a watch or follow stream.
	RequestTimeout time.Duration
	// WatchLease ends each watch and follow after this long: a stream is admitted once, so the lease bounds
	// how long a revoked token keeps reading.
	WatchLease time.Duration
}

// Methods admits a reviewed ServiceAccount to the read API. The handler scopes ordinary callers to
// their retained execution principal and keeps the configured Pod-bound backend readers explicit.
func Methods(resolver *auth.Resolver) auth.Methods {
	rule := auth.ReviewedServiceAccount(resolver)
	return auth.Methods{
		historypb.HistoryService_ReadSessionEvents_FullMethodName:        rule,
		historypb.HistoryService_FollowSession_FullMethodName:            rule,
		historypb.HistoryService_ReadSessionObservations_FullMethodName:  rule,
		historypb.HistoryService_GetSessionSummary_FullMethodName:        rule,
		historypb.HistoryService_BatchGetSessionSummaries_FullMethodName: rule,
		historypb.HistoryService_ReadThreadEntities_FullMethodName:       rule,
		historypb.HistoryService_ReadEntityEvidence_FullMethodName:       rule,
		historypb.HistoryService_ReadNativeFrames_FullMethodName:         rule,
		historypb.HistoryService_FindSessions_FullMethodName:             rule,
		historypb.HistoryService_WatchSessionSummaries_FullMethodName:    rule,
	}
}

func NewService(options Options) *Service {
	draining, drain := context.WithCancel(context.Background())
	readerAccounts := make(map[auth.Account]struct{}, len(options.ReaderAccounts))
	for _, account := range options.ReaderAccounts {
		readerAccounts[account] = struct{}{}
	}
	return &Service{
		reader: options.Reader, readerAccounts: readerAccounts, wakeups: options.Wakeups,
		requestTimeout: options.RequestTimeout, lease: options.WatchLease, draining: draining, drain: drain,
	}
}

// Drain ends every open watch and follow, so a shutdown that waits for open calls is not held up until their leases.
func (s *Service) Drain() { s.drain() }

// serve runs read under the request timeout.
func (s *Service) scopedReader(ctx context.Context) (*reads.Reader, error) {
	caller, err := auth.AdmittedReviewedCaller(ctx)
	if err != nil {
		return nil, err
	}
	if caller.Pod != nil && caller.Pod.IsPodBound() {
		if _, privileged := s.readerAccounts[caller.ServiceAccount.Account]; privileged {
			return s.reader.WithReadScope(storage.AllSessions()), nil
		}
	}
	principal := storage.ExecutionPrincipal{
		Namespace: caller.ServiceAccount.Account.Namespace,
		Name:      caller.ServiceAccount.Account.Name,
		UID:       caller.ServiceAccount.ServiceAccountUID,
	}
	return s.reader.WithReadScope(storage.OwnedBy(principal)), nil
}

func serve[Request, Response any](ctx context.Context, s *Service, request *Request, read func(context.Context, *reads.Reader, *Request) (*Response, error)) (*Response, error) {
	reader, err := s.scopedReader(ctx)
	if err != nil {
		return nil, failure(ctx, err)
	}
	ctx, cancel := context.WithTimeout(ctx, s.requestTimeout)
	defer cancel()
	response, err := read(ctx, reader, request)
	if err != nil {
		return nil, failure(ctx, err)
	}
	return response, nil
}

func (s *Service) ReadSessionEvents(ctx context.Context, request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
	return serve(ctx, s, request, func(ctx context.Context, reader *reads.Reader, request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
		return reader.ReadSessionEvents(ctx, request)
	})
}

func (s *Service) ReadSessionObservations(ctx context.Context, request *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error) {
	return serve(ctx, s, request, func(ctx context.Context, reader *reads.Reader, request *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error) {
		return reader.ReadSessionObservations(ctx, request)
	})
}

func (s *Service) GetSessionSummary(ctx context.Context, request *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	return serve(ctx, s, request, func(ctx context.Context, reader *reads.Reader, request *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
		return reader.GetSessionSummary(ctx, request)
	})
}

func (s *Service) BatchGetSessionSummaries(ctx context.Context, request *historypb.BatchGetSessionSummariesRequest) (*historypb.BatchGetSessionSummariesResponse, error) {
	return serve(ctx, s, request, func(ctx context.Context, reader *reads.Reader, request *historypb.BatchGetSessionSummariesRequest) (*historypb.BatchGetSessionSummariesResponse, error) {
		return reader.BatchGetSessionSummaries(ctx, request)
	})
}

func (s *Service) FindSessions(ctx context.Context, request *historypb.FindSessionsRequest) (*historypb.FindSessionsResponse, error) {
	return serve(ctx, s, request, func(ctx context.Context, reader *reads.Reader, request *historypb.FindSessionsRequest) (*historypb.FindSessionsResponse, error) {
		return reader.FindSessions(ctx, request)
	})
}

func (s *Service) ReadThreadEntities(ctx context.Context, request *historypb.ReadThreadEntitiesRequest) (*historypb.ReadThreadEntitiesResponse, error) {
	return serve(ctx, s, request, func(ctx context.Context, reader *reads.Reader, request *historypb.ReadThreadEntitiesRequest) (*historypb.ReadThreadEntitiesResponse, error) {
		return reader.ReadThreadEntities(ctx, request)
	})
}

func (s *Service) ReadEntityEvidence(ctx context.Context, request *historypb.ReadEntityEvidenceRequest) (*historypb.ReadEntityEvidenceResponse, error) {
	return serve(ctx, s, request, func(ctx context.Context, reader *reads.Reader, request *historypb.ReadEntityEvidenceRequest) (*historypb.ReadEntityEvidenceResponse, error) {
		return reader.ReadEntityEvidence(ctx, request)
	})
}

func (s *Service) ReadNativeFrames(ctx context.Context, request *historypb.ReadNativeFramesRequest) (*historypb.ReadNativeFramesResponse, error) {
	return serve(ctx, s, request, func(ctx context.Context, reader *reads.Reader, request *historypb.ReadNativeFramesRequest) (*historypb.ReadNativeFramesResponse, error) {
		return reader.ReadNativeFrames(ctx, request)
	})
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
