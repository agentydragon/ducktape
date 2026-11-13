// Package api serves the History Service's HistoryService gRPC API: the reads of retained Session history and
// of its folded Threads, raw entry follows and summary change watches.
//
// Methods admits a ServiceAccount proving itself by a bearer reviewed on that call
// (agentplane/docs/grpc_services.md), or a workload SVID naming an Identity, which reads the Sessions
// retained as executing as that Identity incarnation: a public Session UUID is not itself authority to read
// a transcript.
package api

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/reads"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"k8s.io/apimachinery/pkg/util/sets"
)

// Service is the HistoryService server.
type Service struct {
	historypb.UnimplementedHistoryServiceServer
	reader         *reads.Reader
	readerAccounts sets.Set[auth.Account]
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
	// Wakeups listens to raw history, summary changes and Sessions.
	Wakeups *wakeups.Hub
	// RequestTimeout bounds a call's backend work and each write to a watch or follow stream.
	RequestTimeout time.Duration
	// WatchLease ends each watch and follow after this long: a stream is admitted once, so the lease bounds
	// how long a revoked token keeps reading.
	WatchLease time.Duration
}

// Methods admits a reviewed ServiceAccount or an Identity SVID to the read API. The handler scopes
// ordinary callers to their retained execution principal or Identity and keeps the configured
// Pod-bound backend readers explicit.
func Methods(resolver *auth.Resolver) auth.Methods {
	rule := auth.ReviewedServiceAccountOrIdentity(resolver)
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

// MethodsWithDelegation adds an explicit Action actor grant to the configured full RPC names.
func MethodsWithDelegation(
	resolver *auth.Resolver,
	delegator auth.Delegator,
	serviceAccounts auth.ServiceAccountUIDReader,
	identities auth.IdentityUIDReader,
	delegatedMethods []string,
) (auth.Methods, error) {
	methods, err := Methods(resolver).WithDelegation(
		delegatedMethods, []auth.Delegator{delegator}, serviceAccounts, identities,
	)
	if err != nil {
		return nil, fmt.Errorf("configure delegated History methods: %w", err)
	}
	return methods, nil
}

func NewService(options Options) *Service {
	draining, drain := context.WithCancel(context.Background())
	return &Service{
		reader: options.Reader, readerAccounts: sets.New(options.ReaderAccounts...), wakeups: options.Wakeups,
		requestTimeout: options.RequestTimeout, lease: options.WatchLease, draining: draining, drain: drain,
	}
}

// Drain ends every open watch and follow, so a shutdown that waits for open calls is not held up until their leases.
func (s *Service) Drain() { s.drain() }

// serve runs read under the request timeout.
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
	case auth.DelegatedIdentityAdmission:
		return s.reader.WithReadScope(storage.OwnedByIdentity(storage.Identity{
			Name: identity.EffectiveCaller.Name, UID: identity.EffectiveCaller.UID,
		})), nil
	}
	caller, err := auth.EffectiveCaller(ctx)
	if err != nil {
		return nil, err
	}
	account := caller.GetAccount()
	if account == nil || account.GetNamespace() == "" || account.GetName() == "" || caller.GetUid() == "" {
		return nil, status.Error(codes.Unauthenticated, "effective ServiceAccount identity is incomplete")
	}
	if direct, ok := admission.(auth.DirectAdmission); ok && direct.Pod != nil && direct.Pod.IsPodBound() {
		if s.readerAccounts.Has(auth.Account{Namespace: account.GetNamespace(), Name: account.GetName()}) {
			return s.reader.WithReadScope(storage.AllSessions()), nil
		}
	}
	principal := storage.ExecutionPrincipal{
		Namespace: account.GetNamespace(),
		Name:      account.GetName(),
		UID:       caller.GetUid(),
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
