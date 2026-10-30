// Package admin is the egress proxy's cluster-internal read side: recent decisions per subject, when
// they change, and health.
package admin

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// watchWriteTimeout bounds how long a slow reader can hold an egress decision watch.
const watchWriteTimeout = 5 * time.Second

// Decisions reads a subject's retained decisions; a nil subject selects the refusals that never
// authenticated.
type Decisions interface {
	Recent(ctx context.Context, subject *decision.Subject) ([]decision.Record, error)
}

// Changes follows decision commits; see history.Changes.Follow.
type Changes interface {
	Follow(ctx context.Context, changed func() error) error
}

// HistoryHealth reports the decision writer's counters.
type HistoryHealth interface {
	Health() history.Health
}

// Index is read once per call, so the verdict and the failures it rests on agree.
type Index interface {
	Status() watchhealth.Status
}

// Service is the admin gRPC API. Methods admits its callers.
type Service struct {
	egressadminpb.UnimplementedEgressAdminServer
	decisions     Decisions
	changes       Changes
	historyHealth HistoryHealth
	index         Index
	lease         time.Duration
	draining      context.Context
	drain         context.CancelFunc
}

// ServiceOptions are what a Service reads.
type ServiceOptions struct {
	Decisions     Decisions
	Changes       Changes
	HistoryHealth HistoryHealth
	Index         Index
	// WatchLease ends each watch after this long: a stream is admitted once, so the lease bounds how
	// long a revoked token keeps reading.
	WatchLease time.Duration
}

// Methods admits callers to every EgressAdmin RPC: each proves, by a bearer resolver reviews, one of callers.
func Methods(resolver *auth.Resolver, callers []auth.Account) auth.Methods {
	rule := auth.Accounts(resolver, callers)
	return auth.Methods{
		egressadminpb.EgressAdmin_ListDecisions_FullMethodName:  rule,
		egressadminpb.EgressAdmin_WatchDecisions_FullMethodName: rule,
		egressadminpb.EgressAdmin_GetHealth_FullMethodName:      rule,
	}
}

func NewService(options ServiceOptions) *Service {
	draining, drain := context.WithCancel(context.Background())
	return &Service{
		decisions: options.Decisions, changes: options.Changes, historyHealth: options.HistoryHealth,
		index: options.Index, lease: options.WatchLease, draining: draining, drain: drain,
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

// GetHealth is ready only while the index has synced and no watched kind is failing. `synced` alone
// says only that every kind has listed once; the failing kinds are the facts and `ready` the verdict.
func (s *Service) GetHealth(ctx context.Context, _ *egressadminpb.GetHealthRequest) (*egressadminpb.GetHealthResponse, error) {
	index := s.index.Status()
	counters := s.historyHealth.Health()
	failing := make(map[string]*egressadminpb.KindFailure, len(index.Failing))
	for kind, failure := range index.Failing {
		failing[string(kind)] = &egressadminpb.KindFailure{Error: failure.Error, Since: timestamppb.New(failure.Since)}
	}
	return &egressadminpb.GetHealthResponse{
		Ready: index.Available, Synced: index.Synced, Draining: index.Draining,
		DecisionHistory: &egressadminpb.DecisionHistoryHealth{
			Accepted: int64(counters.Accepted), Acknowledged: int64(counters.Acknowledged),
			DroppedOverflow: int64(counters.DroppedOverflow), DroppedUnavailable: int64(counters.DroppedUnavailable),
			DroppedExpired: int64(counters.DroppedExpired), DroppedShutdown: int64(counters.DroppedShutdown),
			WriteFailures: int64(counters.WriteFailures), CleanupFailures: int64(counters.CleanupFailures),
			Available: counters.Available, Queued: int64(counters.Queued), WriterRunning: counters.WriterRunning,
		},
		Failing: failing,
	}, nil
}

func (s *Service) ListDecisions(ctx context.Context, request *egressadminpb.ListDecisionsRequest) (*egressadminpb.ListDecisionsResponse, error) {
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
		sendFailure = streaming.SendWithin(leased, watchWriteTimeout, stream.Send, response)
		return sendFailure
	})
	switch {
	case ctx.Err() != nil:
		return status.FromContextError(ctx.Err()).Err()
	case leased.Err() != nil:
		// The lease ended, or the server is draining; the caller subscribes again.
		return nil
	case errors.Is(sendFailure, streaming.ErrSendTimeout):
		return status.Error(codes.DeadlineExceeded, "decision watch write deadline expired")
	case sendFailure != nil:
		return sendFailure
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
