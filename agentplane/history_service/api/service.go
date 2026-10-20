// Package api serves the History Service's HistoryService gRPC API: the reads of retained Session history and
// of its folded Threads, and the stream that tells a caller when summaries changed.
//
// Every RPC admits only the configured callers, each proving a ServiceAccount by a bearer reviewed on that call
// (agentplane/docs/grpc_services.md): a public Session UUID is not itself authority to read a transcript.
package api

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/history_service/reads"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	historypb "github.com/agentydragon/ducktape/agentplane/proto/history_service"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Service is the HistoryService server.
type Service struct {
	historypb.UnimplementedHistoryServiceServer
	reader         *reads.Reader
	wakeups        *wakeups.Hub
	resolver       *auth.Resolver
	callers        map[auth.Account]struct{}
	requestTimeout time.Duration
	lease          time.Duration
	draining       context.Context
	drain          context.CancelFunc
}

// Options are what a Service reads and whom it admits.
type Options struct {
	Reader *reads.Reader
	// Wakeups must listen to the channels WatchSessionSummaries follows.
	Wakeups *wakeups.Hub
	// Resolver and Callers admit a call: the caller proves a ServiceAccount by a bearer Resolver reviews, and
	// the ServiceAccount must be one of Callers.
	Resolver *auth.Resolver
	Callers  []auth.Account
	// RequestTimeout bounds a call's backend work and each write to a watch stream.
	RequestTimeout time.Duration
	// WatchLease ends each WatchSessionSummaries after this long: a stream is admitted once, so the lease bounds
	// how long a revoked token keeps reading.
	WatchLease time.Duration
}

func NewService(options Options) *Service {
	allowed := make(map[auth.Account]struct{}, len(options.Callers))
	for _, caller := range options.Callers {
		allowed[caller] = struct{}{}
	}
	draining, drain := context.WithCancel(context.Background())
	return &Service{
		reader: options.Reader, wakeups: options.Wakeups, resolver: options.Resolver, callers: allowed,
		requestTimeout: options.RequestTimeout, lease: options.WatchLease, draining: draining, drain: drain,
	}
}

// Drain ends every open watch, so a shutdown that waits for open calls is not held up until their leases.
func (s *Service) Drain() { s.drain() }

// authenticate admits the configured callers only.
func (s *Service) authenticate(ctx context.Context) error {
	if _, err := s.resolver.RequireAccount(ctx, s.callers); err != nil {
		return failure(ctx, err)
	}
	return nil
}

// serve admits the caller, then runs read, both under the request timeout.
func serve[Request, Response any](ctx context.Context, s *Service, request *Request, read func(context.Context, *Request) (*Response, error)) (*Response, error) {
	ctx, cancel := context.WithTimeout(ctx, s.requestTimeout)
	defer cancel()
	if err := s.authenticate(ctx); err != nil {
		return nil, err
	}
	response, err := read(ctx, request)
	if err != nil {
		return nil, failure(ctx, err)
	}
	return response, nil
}

func (s *Service) ReadSessionEvents(ctx context.Context, request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
	return serve(ctx, s, request, s.reader.ReadSessionEvents)
}

func (s *Service) ReadSessionObservations(ctx context.Context, request *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error) {
	return serve(ctx, s, request, s.reader.ReadSessionObservations)
}

func (s *Service) GetSessionSummary(ctx context.Context, request *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	return serve(ctx, s, request, s.reader.GetSessionSummary)
}

func (s *Service) BatchGetSessionSummaries(ctx context.Context, request *historypb.BatchGetSessionSummariesRequest) (*historypb.BatchGetSessionSummariesResponse, error) {
	return serve(ctx, s, request, s.reader.BatchGetSessionSummaries)
}

func (s *Service) FindSessions(ctx context.Context, request *historypb.FindSessionsRequest) (*historypb.FindSessionsResponse, error) {
	return serve(ctx, s, request, s.reader.FindSessions)
}

func (s *Service) ReadThreadEntities(ctx context.Context, request *historypb.ReadThreadEntitiesRequest) (*historypb.ReadThreadEntitiesResponse, error) {
	return serve(ctx, s, request, s.reader.ReadThreadEntities)
}

func (s *Service) ReadEntityEvidence(ctx context.Context, request *historypb.ReadEntityEvidenceRequest) (*historypb.ReadEntityEvidenceResponse, error) {
	return serve(ctx, s, request, s.reader.ReadEntityEvidence)
}

func (s *Service) ReadNativeFrames(ctx context.Context, request *historypb.ReadNativeFramesRequest) (*historypb.ReadNativeFramesResponse, error) {
	return serve(ctx, s, request, s.reader.ReadNativeFrames)
}

// failure answers a failed call. The client gets a fixed message per kind of failure: no SQL, bearer or
// Kubernetes response text reaches it. The cause is logged where the message names none.
func failure(ctx context.Context, err error) error {
	switch {
	case status.Code(err) != codes.Unknown:
		// An admission refusal, or a failure already answered.
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
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, "history read deadline expired")
	case ctx.Err() != nil:
		return status.FromContextError(ctx.Err()).Err()
	default:
		slog.Warn("history read failed", "error", err)
		return status.Error(codes.Unavailable, "history or authentication unavailable")
	}
}
