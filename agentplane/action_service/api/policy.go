package api

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/policyview"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

// GetActionPolicy answers what an account's bindings auto-decide: the caller's own, or the named subject's. Any
// workload may name any account. A service reader's exception covers requests only, so it reads policy as a workload
// does, only while its ServiceAccount carries the caller label. An operator must name the account, and is also shown
// each binding's diagnostics.
func (s *Service) GetActionPolicy(ctx context.Context, request *action_servicepb.GetActionPolicyRequest) (*action_servicepb.GetActionPolicyResponse, error) {
	var own reader.Subject
	viewer := policyview.Workload
	switch principal := admitted(ctx).principal.(type) {
	case store.Owner:
		own = policySubject(principal)
	case store.ServiceReaderPrincipal:
		if !s.gate.callers.Admits(auth.Account(principal.Account)) {
			return nil, unauthenticated
		}
		own = reader.ServiceAccount{Namespace: principal.Account.Namespace, Name: principal.Account.Name}
	case store.OperatorPrincipal:
		viewer = policyview.Operator
	}
	subject := own
	switch named := request.GetSubject(); {
	case named == nil && own == nil:
		return nil, invalidRequest("an operator has no account of its own: name the subject")
	case named == nil:
	case named.GetNamespace() == "" || named.GetName() == "":
		return nil, invalidRequest("subject must set both namespace and name")
	default:
		subject = reader.ServiceAccount{Namespace: named.GetNamespace(), Name: named.GetName()}
	}
	return &action_servicepb.GetActionPolicyResponse{ActionPolicy: policyview.View(s.policy, subject, viewer)}, nil
}

// WatchActionPolicy follows the same view and authorization as GetActionPolicy. Subscribing before each read
// prevents a change between the read and the wait from being lost.
func (s *Service) WatchActionPolicy(request *action_servicepb.GetActionPolicyRequest, stream grpc.ServerStreamingServer[action_servicepb.GetActionPolicyResponse]) error {
	ctx := stream.Context()
	writeCtx, stopWrites := watchWriteContext(ctx, s.gate)
	defer stopWrites()
	recheck := time.NewTicker(recheckInterval)
	defer recheck.Stop()
	var previous *action_servicepb.GetActionPolicyResponse
	for {
		rulesChanged := s.policy.Rules.Changed()
		healthChanged := s.policy.Health.Changed()
		current, err := s.GetActionPolicy(ctx, request)
		if err != nil {
			return err
		}
		if !proto.Equal(previous, current) {
			if err := send(ctx, writeCtx, s.gate, stream, current); err != nil {
				return err
			}
			previous = current
		}
		select {
		case <-rulesChanged:
		case <-healthChanged:
		case <-recheck.C:
			if err := s.gate.Recheck(ctx); err != nil {
				return err
			}
		case <-s.gate.DrainingContext().Done():
			return errDraining
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}
