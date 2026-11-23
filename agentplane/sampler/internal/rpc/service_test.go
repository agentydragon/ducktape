package rpc

import (
	"errors"
	"strings"
	"testing"

	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/protobuf/proto"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestAdmissionRequiresAllowedServiceSVID(t *testing.T) {
	for _, tc := range []struct {
		name, actor string
		bearer      bool
		noPeer      bool
		want        codes.Code
	}{
		{name: "accepted service", actor: peertls.SessionsURI, want: codes.OK},
		{name: "unlisted service", actor: peertls.ActionsURI, want: codes.PermissionDenied},
		{name: "former combined service role", actor: peertls.SessionDispatcherURI, want: codes.PermissionDenied},
		{name: "bearer beside certificate", actor: peertls.SessionsURI, bearer: true, want: codes.Unauthenticated},
		{name: "bearer without certificate", bearer: true, noPeer: true, want: codes.Unauthenticated},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx := t.Context()
			if !tc.noPeer {
				ctx = serviceContext(t, tc.actor)
			}
			if tc.bearer {
				ctx = metadata.NewIncomingContext(ctx, metadata.Pairs("authorization", "Bearer test-token"))
			}
			settings := fixtureSettings()
			settings.ServiceCallers = []string{peertls.SessionsURI}
			sampler := Sampler{Settings: settings}
			admitted, cancel, owner, err := sampler.admitted(ctx)
			defer cancel()
			if status.Code(err) != tc.want {
				t.Fatalf("admission=%v,want=%s", err, tc.want)
			}
			if tc.want == codes.OK {
				if owner != peertls.SessionsURI {
					t.Fatalf("owner=%q", owner)
				}
				if _, err := auth.Caller(admitted); err == nil {
					t.Fatal("service obtained account proof")
				}
			}
		})
	}
}

func TestSamplerAdminUsesSeparateServiceGrant(t *testing.T) {
	settings := fixtureSettings()
	settings.ServiceCallers = []string{peertls.SessionsURI}
	settings.AdminServiceCallers = []string{peertls.ActionsURI}
	admin := &SamplerAdmin{Sampler: Sampler{Settings: settings}}
	for _, tc := range []struct {
		actor string
		want  codes.Code
	}{
		{actor: peertls.SessionsURI, want: codes.PermissionDenied},
		{actor: peertls.ActionsURI, want: codes.OK},
	} {
		ctx := serviceContext(t, tc.actor)
		_, cancel, principal, err := admin.admitted(ctx)
		cancel()
		if status.Code(err) != tc.want {
			t.Fatalf("admin admission for %s=%v, want %s", tc.actor, err, tc.want)
		}
		if tc.want == codes.OK && principal != tc.actor {
			t.Fatalf("admin principal=%q, want %q", principal, tc.actor)
		}
	}
}

func TestSamplerErrorsExposeFixedStatus(t *testing.T) {
	for _, c := range []struct {
		err  error
		code codes.Code
	}{{sessionstore.ErrForbidden, codes.PermissionDenied}, {sessionstore.ErrConflict, codes.AlreadyExists}, {protocol.ErrMissing, codes.NotFound}, {protocol.ErrPrecondition, codes.FailedPrecondition}, {protocol.ErrInvalid, codes.InvalidArgument}, {errors.New("secret provider key"), codes.Unavailable}} {
		err := samplerError(t.Context(), c.err)
		if status.Code(err) != c.code {
			t.Fatalf("%v: %v", c.err, err)
		}
		if status.Convert(err).Message() == c.err.Error() {
			t.Fatalf("error detail leaked: %v", err)
		}
	}
}

func TestSessionConfigurationDerivesExecutor(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mutate func(*harnesshostpb.CreateSessionRequest)
		want   codes.Code
	}{
		{name: "placement", want: codes.OK},
		{name: "missing placement", mutate: func(r *harnesshostpb.CreateSessionRequest) { r.Spec.Placement = nil }, want: codes.InvalidArgument},
		{name: "missing executor", mutate: func(r *harnesshostpb.CreateSessionRequest) {
			r.Spec.Placement.GetCentralSamplerV1().ExecutorEnvironment = nil
		}, want: codes.InvalidArgument},
		{name: "missing name", mutate: func(r *harnesshostpb.CreateSessionRequest) {
			r.Spec.Placement.GetCentralSamplerV1().ExecutorEnvironment.Name = ""
		}, want: codes.InvalidArgument},
		{name: "missing UID", mutate: func(r *harnesshostpb.CreateSessionRequest) {
			r.Spec.Placement.GetCentralSamplerV1().ExecutorEnvironment.Uid = ""
		}, want: codes.InvalidArgument},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sampler := Sampler{Settings: fixtureSettings()}
			request := &harnesshostpb.CreateSessionRequest{
				SessionId: "test-session", Spec: &sessionpb.SessionSpec{Cwd: "/fixture", Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: "executor", Uid: "sandbox-uid"}}}}},
			}
			if tc.mutate != nil {
				tc.mutate(request)
			}
			original := proto.Clone(request)
			configuration, err := sampler.sessionConfiguration(request)
			if status.Code(err) != tc.want {
				t.Fatalf("configuration=%v err=%v, want %s", configuration, err, tc.want)
			}
			if !proto.Equal(original, request) {
				t.Fatal("caller request mutated")
			}
			if tc.want == codes.OK {
				destination := configuration.GetExecutor()
				if destination.GetTarget().GetNamespace() != "fixture" || destination.GetTarget().GetName() != "executor" || destination.GetTarget().GetUid() != "sandbox-uid" || destination.GetOwner().GetNamespace() != "fixture" || destination.GetOwner().GetName() != "executor" || configuration.GetExpectedIncarnationUid() != "" {
					t.Fatalf("configuration=%v", configuration)
				}
			}
		})
	}
}

func TestCreateSessionReportsUnsupportedModelAndEffort(t *testing.T) {
	for _, tc := range []struct {
		name, model, effort, field string
	}{
		{name: "unknown model", model: "missing-model", effort: "high", field: "model"},
		{name: "unsupported effort", model: "test-model", effort: "medium", field: "reasoning effort"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sampler := Sampler{Settings: fixtureSettings()}
			request := &harnesshostpb.CreateSessionRequest{
				SessionId: "b59b1e1b-f98e-4eef-88c4-c98bb458163a",
				Spec: &sessionpb.SessionSpec{
					Cwd: "/fixture", Model: tc.model, ReasoningEffort: tc.effort,
					Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: "executor", Uid: "sandbox-uid"}}}},
				},
			}
			_, err := sampler.CreateSession(serviceContext(t, peertls.SessionsURI), request)
			if status.Code(err) != codes.InvalidArgument || !strings.Contains(status.Convert(err).Message(), tc.field) {
				t.Fatalf("CreateSession error = %v, want InvalidArgument identifying %s", err, tc.field)
			}
		})
	}
}
