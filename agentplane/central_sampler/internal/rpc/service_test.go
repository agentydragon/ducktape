package rpc

import (
	"errors"
	"strings"
	"testing"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/protobuf/proto"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
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

func TestSessionConfigurationDerivesExecutorAndGatesLegacyOmission(t *testing.T) {
	for _, tc := range []struct {
		name            string
		configured      bool
		omitExecutor    bool
		omitIncarnation bool
		mutate          func(*samplerpb.CreateSessionRequest)
		want            codes.Code
	}{
		{name: "legacy", want: codes.OK},
		{name: "configured legacy", configured: true, want: codes.OK},
		{name: "placement only", configured: true, omitExecutor: true, omitIncarnation: true, want: codes.OK},
		{name: "configured missing executor", configured: true, omitExecutor: true, want: codes.OK},
		{name: "configured missing incarnation", configured: true, omitIncarnation: true, want: codes.OK},
		{name: "unconfigured missing executor", omitExecutor: true, want: codes.FailedPrecondition},
		{name: "unconfigured missing incarnation", omitIncarnation: true, want: codes.FailedPrecondition},
		{name: "unconfigured missing both", omitExecutor: true, omitIncarnation: true, want: codes.FailedPrecondition},
		{name: "mismatched name", mutate: func(r *samplerpb.CreateSessionRequest) { r.Executor.Target.Name = "other" }, want: codes.InvalidArgument},
		{name: "mismatched UID", mutate: func(r *samplerpb.CreateSessionRequest) { r.Executor.Target.Uid = "other" }, want: codes.InvalidArgument},
		{name: "mismatched namespace", configured: true, mutate: func(r *samplerpb.CreateSessionRequest) {
			r.Executor.Target.Namespace = "other"
			r.Executor.Owner.Namespace = "other"
		}, want: codes.InvalidArgument},
		{name: "mismatched owner", mutate: func(r *samplerpb.CreateSessionRequest) { r.Executor.Owner.Name = "other" }, want: codes.InvalidArgument},
		{name: "missing placement", configured: true, mutate: func(r *samplerpb.CreateSessionRequest) { r.Spec.Placement = nil }, want: codes.InvalidArgument},
		{name: "missing executor placement", configured: true, mutate: func(r *samplerpb.CreateSessionRequest) {
			r.Spec.Placement.GetCentralSamplerV1().ExecutorEnvironment = nil
		}, want: codes.InvalidArgument},
	} {
		t.Run(tc.name, func(t *testing.T) {
			namespace := "test"
			sampler := Sampler{Settings: fixtureSettings()}
			if tc.configured {
				sampler.Settings.EnvironmentNamespace = &namespace
			}
			request := &samplerpb.CreateSessionRequest{
				SessionId: "test-session", Spec: &sessionpb.SessionSpec{Cwd: "/fixture", Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: "executor", Uid: "sandbox-uid"}}}}},
				Executor:               &identitypb.SandboxDestination{Owner: &identitypb.ServiceAccount{Namespace: namespace, Name: "executor"}, Target: &identitypb.EnvironmentTarget{Namespace: namespace, Name: "executor", Uid: "sandbox-uid"}},
				ExpectedIncarnationUid: "dispatcher-incarnation",
			}
			if tc.omitExecutor {
				request.Executor = nil
			}
			if tc.omitIncarnation {
				request.ExpectedIncarnationUid = ""
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
				if configuration.GetExecutor().GetTarget().GetNamespace() != namespace || configuration.GetExecutor().GetTarget().GetUid() != "sandbox-uid" || configuration.GetExecutor().GetOwner().GetName() != "executor" || configuration.GetExpectedIncarnationUid() != "" {
					t.Fatalf("configuration=%v", configuration)
				}
			} else if tc.want == codes.FailedPrecondition && !strings.Contains(status.Convert(err).Message(), "environment_namespace") {
				t.Fatalf("missing rollout detail: %v", err)
			}
		})
	}
}
