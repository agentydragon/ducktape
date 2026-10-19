package admin

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	egressadminpb "github.com/agentydragon/ducktape/agentplane/proto/egress_admin"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Service is the HTTP admin API's reads as gRPC. It takes the same Decisions, Changes, HistoryHealth
// and Index, so what the two serve is read from one place.
//
// Deviation from the HTTP API: the HTTP admin listener is fenced by network policy alone, while every
// RPC here admits only the allowed callers, each proving a ServiceAccount by a bearer reviewed on
// that call (agentplane/docs/grpc_services.md).
type Service struct {
	egressadminpb.UnimplementedEgressAdminServer
	decisions     Decisions
	changes       Changes
	historyHealth HistoryHealth
	index         Index
	clock         func() time.Time
	resolver      *auth.Resolver
	callers       map[auth.Account]struct{}
	lease         time.Duration
	draining      context.Context
	drain         context.CancelFunc
}

// ServiceOptions are what a Service reads and whom it admits.
type ServiceOptions struct {
	Decisions     Decisions
	Changes       Changes
	HistoryHealth HistoryHealth
	Index         Index
	Clock         func() time.Time
	// Resolver and Callers admit a call: the caller proves a ServiceAccount by a bearer Resolver
	// reviews, and the ServiceAccount must be one of Callers.
	Resolver *auth.Resolver
	Callers  []auth.Account
	// WatchLease ends each watch after this long: a stream is admitted once, so the lease bounds how
	// long a revoked token keeps reading.
	WatchLease time.Duration
}

func NewService(options ServiceOptions) *Service {
	allowed := make(map[auth.Account]struct{}, len(options.Callers))
	for _, caller := range options.Callers {
		allowed[caller] = struct{}{}
	}
	draining, drain := context.WithCancel(context.Background())
	return &Service{
		decisions: options.Decisions, changes: options.Changes, historyHealth: options.HistoryHealth,
		index: options.Index, clock: options.Clock, resolver: options.Resolver, callers: allowed,
		lease: options.WatchLease, draining: draining, drain: drain,
	}
}

// Drain ends every open watch, so a shutdown that waits for open calls is not held up until their leases.
func (s *Service) Drain() { s.drain() }

var errHistoryUnavailable = status.Error(codes.Unavailable, "decision-history-unavailable")

// historyFailure answers a failed history read. The error can carry connection details; the response
// and the log name only its type.
func historyFailure(err error) error {
	slog.Warn("decision history read failed", "error_type", fmt.Sprintf("%T", err))
	return errHistoryUnavailable
}

// GetHealth reports what `/healthz` does: its facts, and its verdict as `ready`.
func (s *Service) GetHealth(ctx context.Context, _ *egressadminpb.GetHealthRequest) (*egressadminpb.GetHealthResponse, error) {
	if _, err := s.resolver.RequireAccount(ctx, s.callers); err != nil {
		return nil, err
	}
	body, ready := readHealth(s.index, s.historyHealth, s.clock())
	return &egressadminpb.GetHealthResponse{
		Ready: ready, Synced: body.Synced, Draining: body.Draining,
		DecisionHistory: &egressadminpb.DecisionHistoryHealth{
			Accepted: int64(body.DecisionHistory.Accepted), Acknowledged: int64(body.DecisionHistory.Acknowledged),
			DroppedOverflow: int64(body.DecisionHistory.DroppedOverflow), DroppedUnavailable: int64(body.DecisionHistory.DroppedUnavailable),
			DroppedExpired: int64(body.DecisionHistory.DroppedExpired), DroppedShutdown: int64(body.DecisionHistory.DroppedShutdown),
			WriteFailures: int64(body.DecisionHistory.WriteFailures), CleanupFailures: int64(body.DecisionHistory.CleanupFailures),
			Available: body.DecisionHistory.Available, Queued: int64(body.DecisionHistory.Queued),
			WriterRunning: body.DecisionHistory.WriterRunning,
		},
		StaleAfterSeconds: body.StaleAfterSeconds, RefreshedSecondsAgo: body.RefreshedSecondsAgo,
	}, nil
}

func (s *Service) ListDecisions(ctx context.Context, request *egressadminpb.ListDecisionsRequest) (*egressadminpb.ListDecisionsResponse, error) {
	if _, err := s.resolver.RequireAccount(ctx, s.callers); err != nil {
		return nil, err
	}
	subject, err := subjectOf(request.GetFilter())
	if err != nil {
		return nil, err
	}
	records, err := s.decisions.Recent(ctx, subject)
	if err != nil {
		return nil, historyFailure(err)
	}
	return &egressadminpb.ListDecisionsResponse{Decisions: decisionsOf(records)}, nil
}

// WatchDecisions sends the list on subscribing and again whenever it differs from the last one sent.
// Changes only prompts the re-read, so a heartbeat of quiet or a commit by another subject sends nothing.
func (s *Service) WatchDecisions(request *egressadminpb.WatchDecisionsRequest, stream grpc.ServerStreamingServer[egressadminpb.WatchDecisionsResponse]) error {
	ctx := stream.Context()
	if _, err := s.resolver.RequireAccount(ctx, s.callers); err != nil {
		return err
	}
	subject, err := subjectOf(request.GetFilter())
	if err != nil {
		return err
	}
	leased, endLease := context.WithTimeout(ctx, s.lease)
	defer endLease()
	defer context.AfterFunc(s.draining, endLease)()
	var last *egressadminpb.WatchDecisionsResponse
	var sendFailure error
	err = s.changes.Follow(leased, func() error {
		records, err := s.decisions.Recent(leased, subject)
		if err != nil {
			return err
		}
		response := &egressadminpb.WatchDecisionsResponse{Decisions: decisionsOf(records)}
		if last != nil && proto.Equal(last, response) {
			return nil
		}
		last = response
		sendFailure = stream.Send(response)
		return sendFailure
	})
	switch {
	case sendFailure != nil:
		return sendFailure
	case ctx.Err() != nil:
		return status.FromContextError(ctx.Err()).Err()
	case leased.Err() != nil:
		// The lease ended, or the server is draining; the caller subscribes again.
		return nil
	default:
		return historyFailure(err)
	}
}

// subjectOf is the decisions' subject the filter selects; nil selects the refusals that never
// authenticated.
func subjectOf(filter *egressadminpb.DecisionFilter) (*decision.Subject, error) {
	switch scope := filter.GetScope().(type) {
	case *egressadminpb.DecisionFilter_Subject:
		if scope.Subject.GetNamespace() == "" || scope.Subject.GetName() == "" {
			return nil, status.Error(codes.InvalidArgument, "subject needs a namespace and a name")
		}
		return &decision.Subject{Namespace: scope.Subject.GetNamespace(), Name: scope.Subject.GetName()}, nil
	case *egressadminpb.DecisionFilter_Unauthenticated:
		return nil, nil
	default:
		return nil, status.Error(codes.InvalidArgument, "filter must select a subject or the unauthenticated")
	}
}

func decisionsOf(records []decision.Record) []*egressadminpb.Decision {
	decisions := make([]*egressadminpb.Decision, len(records))
	for i, record := range records {
		decisions[i] = &egressadminpb.Decision{
			At: timestamppb.New(record.At), Method: record.Method, Host: record.Host, Port: int32(record.Port),
			Outcome: outcomeOf(record.Outcome), Binding: record.Binding, Policy: record.Policy,
			Substituted: record.Substituted, Address: record.Address,
		}
		if record.Reason != nil {
			decisions[i].Reason = proto.String(string(*record.Reason))
		}
		if record.Rule != nil {
			decisions[i].Rule = proto.Int32(int32(*record.Rule))
		}
	}
	return decisions
}

func outcomeOf(outcome decision.Outcome) egressadminpb.Decision_Outcome {
	switch outcome {
	case decision.Allow:
		return egressadminpb.Decision_OUTCOME_ALLOW
	case decision.Deny:
		return egressadminpb.Decision_OUTCOME_DENY
	}
	// A replica of a newer release may have recorded an outcome this one does not name.
	return egressadminpb.Decision_OUTCOME_UNSPECIFIED
}
