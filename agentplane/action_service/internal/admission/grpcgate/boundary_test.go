package grpcgate

import (
	"context"
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/identity/operator"
	"github.com/agentydragon/ducktape/agentplane/identity/operator/testissuer"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type contextStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s contextStream) Context() context.Context { return s.ctx }

func TestErrorMappingAndDrainBoundary(t *testing.T) {
	issuer := testissuer.New(t)
	verifier, err := operator.NewVerifier(issuer.Settings(operator.ProfileAuthentik))
	if err != nil {
		t.Fatal(err)
	}
	ctx := metadata.NewIncomingContext(t.Context(), metadata.Pairs("authorization", "Bearer "+issuer.Token(t, nil)))
	handlerError := errors.New("test handler failure")
	mapped := status.Error(codes.Aborted, "test mapped failure")
	var calls int
	var gotError error
	var gotContext context.Context
	gate := New(Options{Operators: verifier, ErrorMapper: func(ctx context.Context, err error) error {
		calls++
		gotContext, gotError = ctx, err
		return mapped
	}})
	unary := &grpc.UnaryServerInfo{FullMethod: action_servicepb.ActionService_ListActionRequests_FullMethodName}
	handler := func(ctx context.Context, _ any) (any, error) {
		principal, ok := Admitted(ctx).Principal.(store.OperatorPrincipal)
		if !ok || principal.Subject != testissuer.Subject || principal.Issuer != issuer.URL {
			t.Fatalf("admission: %+v", Admitted(ctx))
		}
		return nil, handlerError
	}
	if _, err := gate.UnaryInterceptor(ctx, nil, unary, handler); err != mapped || calls != 1 || gotError != handlerError {
		t.Fatalf("unary mapping: err=%v calls=%d cause=%v", err, calls, gotError)
	}
	if principal, ok := Admitted(gotContext).Principal.(store.OperatorPrincipal); !ok || principal.Subject != testissuer.Subject {
		t.Fatal("mapper lost admission")
	}
	// Failed authentication and absent method grants never reach the handler or domain mapper.
	if _, err := gate.UnaryInterceptor(t.Context(), nil, unary, handler); status.Code(err) != codes.Unauthenticated || calls != 1 {
		t.Fatalf("authentication: err=%v calls=%d", err, calls)
	}
	if _, err := gate.UnaryInterceptor(ctx, nil, &grpc.UnaryServerInfo{FullMethod: "/test.unlisted.Service/Call"}, handler); status.Code(err) != codes.PermissionDenied || calls != 1 {
		t.Fatalf("allowlist: err=%v calls=%d", err, calls)
	}
	if _, err := gate.UnaryInterceptor(ctx, nil, unary, func(context.Context, any) (any, error) { return "test response", nil }); err != nil || calls != 1 {
		t.Fatalf("successful unary mapped: %v, calls=%d", err, calls)
	}
	stream := &grpc.StreamServerInfo{FullMethod: action_servicepb.ActionService_WatchActionRequests_FullMethodName}
	if err := gate.StreamInterceptor(nil, contextStream{ctx: ctx}, stream, func(_ any, s grpc.ServerStream) error { return handlerError }); err != mapped || calls != 2 || gotError != handlerError {
		t.Fatalf("stream mapping: err=%v calls=%d cause=%v", err, calls, gotError)
	}
	if err := gate.StreamInterceptor(nil, contextStream{ctx: ctx}, stream, func(_ any, s grpc.ServerStream) error { return nil }); err != mapped || calls != 3 || gotError != nil {
		t.Fatalf("successful stream mapping: err=%v calls=%d cause=%v", err, calls, gotError)
	}
	gate.Drain()
	if gate.DrainingContext().Err() != context.Canceled {
		t.Fatal("drain did not cancel lifetime")
	}
	if _, err := gate.UnaryInterceptor(ctx, nil, unary, handler); err != mapped || calls != 4 || !errors.Is(gotError, ErrDraining) {
		t.Fatalf("drain mapping: err=%v calls=%d cause=%v", err, calls, gotError)
	}
}
