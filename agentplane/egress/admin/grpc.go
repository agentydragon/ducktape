// Package admin is the egress proxy's cluster-internal read side: recent decisions per subject, when
// they change, and health.
package admin

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// watchWriteTimeout bounds how long a slow reader can hold an egress decision watch.
const watchWriteTimeout = 5 * time.Second

// Decisions reads a subject's retained decisions; a nil subject selects the refusals that never
// authenticated.
type Decisions interface {
	Recent(ctx context.Context, subject decision.Subject) ([]decision.Record, error)
	ByEventIDs(ctx context.Context, eventIDs []uuid.UUID) ([]decision.Record, error)
	Readable(ctx context.Context) error
	Window() history.Window
}

// Changes follows decision commits; see history.Changes.Follow.
type Changes interface {
	Follow(ctx context.Context, subject decision.Subject, changed func(history.Change) error) error
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
	windows       *decisionWindows
	historyHealth HistoryHealth
	index         Index
	credentials   *policy.Index
	draining      context.Context
	drain         context.CancelFunc
}

// ServiceOptions are what a Service reads.
type ServiceOptions struct {
	Decisions     Decisions
	Changes       Changes
	HistoryHealth HistoryHealth
	Index         Index
	Credentials   *policy.Index
}

// Methods admits callers to every EgressAdmin RPC: each proves, by a bearer resolver reviews, one of callers.
func Methods(resolver *auth.Resolver, callers []auth.Account) auth.Methods {
	rule := auth.Accounts(resolver, callers)
	return auth.Methods{
		egressadminpb.EgressAdmin_ListDecisions_FullMethodName:    rule,
		egressadminpb.EgressAdmin_WatchDecisions_FullMethodName:   rule,
		egressadminpb.EgressAdmin_GetHealth_FullMethodName:        rule,
		egressadminpb.EgressAdmin_WatchCredentials_FullMethodName: rule,
	}
}

func NewService(options ServiceOptions) *Service {
	draining, drain := context.WithCancel(context.Background())
	var windows *decisionWindows
	if options.Decisions != nil && options.Changes != nil {
		windows = newDecisionWindows(options.Decisions, options.Changes)
	}
	return &Service{
		decisions: options.Decisions, changes: options.Changes, historyHealth: options.HistoryHealth,
		index: options.Index, credentials: options.Credentials, draining: draining, drain: drain,
		windows: windows,
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
	failing := make([]*egressadminpb.KindFailure, 0, len(index.Failing))
	for kind, failure := range index.Failing {
		failing = append(failing, &egressadminpb.KindFailure{Kind: kindOf(kind), Error: failure.Error, Since: timestamppb.New(failure.Since)})
	}
	slices.SortFunc(failing, func(a, b *egressadminpb.KindFailure) int { return cmp.Compare(a.GetKind(), b.GetKind()) })
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

// WatchDecisions sends an initial recent window, then changes to it. A quiet feed sends
// heartbeats only after the history read succeeds. No watch lease interrupts a healthy stream.
func (s *Service) WatchDecisions(request *egressadminpb.WatchDecisionsRequest, stream grpc.ServerStreamingServer[egressadminpb.WatchDecisionsResponse]) error {
	return s.watchDecisions(request, stream, auth.RevalidateStream)
}

func (s *Service) watchDecisions(request *egressadminpb.WatchDecisionsRequest, stream grpc.ServerStreamingServer[egressadminpb.WatchDecisionsResponse], revalidate func(context.Context) error) error {
	ctx := stream.Context()
	subject, err := subjectOf(request.GetFilter())
	if err != nil {
		return err
	}
	watchCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	defer context.AfterFunc(s.draining, cancel)()
	if s.windows == nil {
		return historyFailure(errors.New("decision history feed is unavailable"))
	}
	subscription, snapshot, err := s.windows.subscribe(watchCtx, subject)
	if err != nil {
		if ctx.Err() != nil {
			return status.FromContextError(ctx.Err()).Err()
		}
		if s.draining.Err() != nil {
			return nil
		}
		return historyFailure(err)
	}
	defer s.windows.unsubscribe(subject, subscription)
	if err := revalidate(ctx); err != nil {
		return err
	}
	if err := streaming.SendWithin(
		watchCtx, watchWriteTimeout, stream.Send,
		&egressadminpb.WatchDecisionsResponse{Frame: &egressadminpb.WatchDecisionsResponse_Snapshot{
			Snapshot: &egressadminpb.DecisionSnapshot{Decisions: snapshot},
		}},
	); err != nil {
		return watchWriteError(ctx, s.draining, err)
	}
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case <-s.draining.Done():
			return nil
		case <-ticker.C:
			if err := revalidate(ctx); err != nil {
				return err
			}
		case <-subscription.done:
			if subscription.err == nil {
				subscription.err = errors.New("decision history feed ended")
			}
			return historyFailure(subscription.err)
		case update := <-subscription.updates:
			if err := revalidate(ctx); err != nil {
				return err
			}
			var frame *egressadminpb.WatchDecisionsResponse
			if update.delta != nil {
				frame = &egressadminpb.WatchDecisionsResponse{Frame: &egressadminpb.WatchDecisionsResponse_Delta{Delta: update.delta}}
			} else if update.heartbeat {
				frame = &egressadminpb.WatchDecisionsResponse{Frame: &egressadminpb.WatchDecisionsResponse_Heartbeat{Heartbeat: &emptypb.Empty{}}}
			} else {
				continue
			}
			if err := streaming.SendWithin(watchCtx, watchWriteTimeout, stream.Send, frame); err != nil {
				return watchWriteError(ctx, s.draining, err)
			}
		}
	}
}

func watchWriteError(ctx, draining context.Context, err error) error {
	if ctx.Err() != nil {
		return status.FromContextError(ctx.Err()).Err()
	}
	if draining.Err() != nil {
		return nil
	}
	if errors.Is(err, streaming.ErrSendTimeout) {
		return status.Error(codes.DeadlineExceeded, "decision watch write deadline expired")
	}
	return err
}

// decisionDelta compares bounded windows by their durable event identity. This also reports
// late commits and expiry, which cannot safely be represented by an advancing timestamp cursor.
func decisionDelta(previous, current []*egressadminpb.Decision) *egressadminpb.DecisionDelta {
	old := make(map[string]bool, len(previous))
	fresh := make(map[string]bool, len(current))
	for _, item := range previous {
		old[item.GetEventId()] = true
	}
	for _, item := range current {
		fresh[item.GetEventId()] = true
	}
	delta := &egressadminpb.DecisionDelta{}
	for _, item := range current {
		if !old[item.GetEventId()] {
			delta.Added = append(delta.Added, item)
		}
	}
	for _, item := range previous {
		if !fresh[item.GetEventId()] {
			delta.RemovedEventIds = append(delta.RemovedEventIds, item.GetEventId())
		}
	}
	return delta
}

// subjectOf is the decisions' subject the filter selects; nil selects the refusals that never
// authenticated.
func subjectOf(filter *egressadminpb.DecisionFilter) (decision.Subject, error) {
	switch scope := filter.GetScope().(type) {
	case *egressadminpb.DecisionFilter_Subject:
		if scope.Subject.GetNamespace() == "" || scope.Subject.GetName() == "" {
			return nil, status.Error(codes.InvalidArgument, "subject needs a namespace and a name")
		}
		return decision.ServiceAccount{Namespace: scope.Subject.GetNamespace(), Name: scope.Subject.GetName()}, nil
	case *egressadminpb.DecisionFilter_Identity:
		if scope.Identity.GetName() == "" {
			return nil, status.Error(codes.InvalidArgument, "identity needs a name")
		}
		return decision.Identity{Name: scope.Identity.GetName()}, nil
	case *egressadminpb.DecisionFilter_Unauthenticated:
		return nil, nil
	default:
		return nil, status.Error(codes.InvalidArgument, "filter must select a subject, an identity or the unauthenticated")
	}
}

func decisionsOf(records []decision.Record) []*egressadminpb.Decision {
	decisions := make([]*egressadminpb.Decision, len(records))
	for i, record := range records {
		decisions[i] = &egressadminpb.Decision{
			EventId: record.EventID.String(), At: timestamppb.New(record.At), Method: record.Method, Host: record.Host, Port: int32(record.Port),
			Outcome: outcomeOf(record.Outcome), Binding: record.Binding, Policy: record.Policy,
			Substituted: record.Substituted, Address: record.Address,
		}
		if record.Impersonated != nil {
			decisions[i].Impersonated = &identitypb.ServiceAccount{
				Namespace: record.Impersonated.Namespace, Name: record.Impersonated.Name,
			}
		}
		if record.Reason != nil {
			decisions[i].Reason = reasonOf(*record.Reason).Enum()
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

func reasonOf(reason decision.Reason) egressadminpb.Decision_Reason {
	switch reason {
	case decision.TokenMissing:
		return egressadminpb.Decision_REASON_TOKEN_MISSING
	case decision.TokenRejected:
		return egressadminpb.Decision_REASON_TOKEN_REJECTED
	case decision.PodMismatch:
		return egressadminpb.Decision_REASON_POD_MISMATCH
	case decision.NoBinding:
		return egressadminpb.Decision_REASON_NO_BINDING
	case decision.NoRule:
		return egressadminpb.Decision_REASON_NO_RULE
	case decision.InvalidBody:
		return egressadminpb.Decision_REASON_INVALID_BODY
	case decision.PlaceholderUnresolved:
		return egressadminpb.Decision_REASON_PLACEHOLDER_UNRESOLVED
	case decision.CredentialUnavailable:
		return egressadminpb.Decision_REASON_CREDENTIAL_UNAVAILABLE
	case decision.ImpersonationForbidden:
		return egressadminpb.Decision_REASON_IMPERSONATION_FORBIDDEN
	case decision.AddressForbidden:
		return egressadminpb.Decision_REASON_ADDRESS_FORBIDDEN
	case decision.HostUnresolved:
		return egressadminpb.Decision_REASON_HOST_UNRESOLVED
	case decision.Unavailable:
		return egressadminpb.Decision_REASON_UNAVAILABLE
	}
	// A replica of a newer release may have recorded a reason this one does not name.
	return egressadminpb.Decision_REASON_UNKNOWN
}

func kindOf(kind watchhealth.Kind) egressadminpb.KindFailure_Kind {
	switch kind {
	case reader.PolicySets:
		return egressadminpb.KindFailure_KIND_POLICY_SETS
	case reader.PolicyBindings:
		return egressadminpb.KindFailure_KIND_POLICY_BINDINGS
	case reader.Identities:
		return egressadminpb.KindFailure_KIND_IDENTITIES
	case policy.Credentials:
		return egressadminpb.KindFailure_KIND_EGRESS_CREDENTIALS
	case policy.Secrets:
		return egressadminpb.KindFailure_KIND_SECRETS
	}
	// The informers' kinds are named by request path; its error says which request failed.
	return egressadminpb.KindFailure_KIND_UNKNOWN
}
