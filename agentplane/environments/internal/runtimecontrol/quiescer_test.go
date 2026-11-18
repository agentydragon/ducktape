package runtimecontrol

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"sync/atomic"
	"testing"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type quiesceGateway struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	handle func(context.Context, *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error)
}

func (g quiesceGateway) Quiesce(ctx context.Context, request *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error) {
	return g.handle(ctx, request)
}

func newQuiescerTest(t *testing.T, handle func(context.Context, *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error), serverOptions ...grpc.ServerOption) (GatewayQuiescer, *sandboxapi.Sandbox) {
	t.Helper()
	server := grpc.NewServer(serverOptions...)
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, quiesceGateway{handle: handle})
	address := grpcserver.Serve(t, server)
	tokenFile := filepath.Join(t.TempDir(), "controller-token")
	if err := os.WriteFile(tokenFile, []byte("test-controller-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	sandbox := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-sandbox-uid"},
		Status:     sandboxapi.SandboxStatus{Incarnation: &sandboxapi.Incarnation{UID: "test-pod-uid"}},
	}
	return GatewayQuiescer{Target: address, TokenFile: tokenFile}, sandbox
}

func TestGatewayQuiescerRetriesUnavailableForPinnedIncarnation(t *testing.T) {
	type observedCall struct {
		metadata metadata.MD
		request  *sessionpb.QuiesceRequest
	}
	observed := make(chan observedCall, 2)
	var calls atomic.Int32
	quiescer, sandbox := newQuiescerTest(t, func(ctx context.Context, request *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error) {
		md, _ := metadata.FromIncomingContext(ctx)
		observed <- observedCall{metadata: md.Copy(), request: request}
		if calls.Add(1) == 1 {
			return nil, status.Error(codes.Unavailable, "test route is not ready")
		}
		return &sessionpb.QuiesceResponse{Stage: sessionpb.QuiesceStage_QUIESCE_STAGE_QUIESCED}, nil
	})
	idleSince := time.Date(2026, time.January, 1, 0, 0, 0, 123000000, time.UTC)
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	result, err := quiescer.Quiesce(ctx, sandbox, &idleSince)
	if err != nil || result.Quiescing {
		t.Fatalf("restored route did not finish quiescing: result=%v err=%v", result, err)
	}
	if calls.Load() != 2 {
		t.Fatalf("Quiesce calls = %d, want 2", calls.Load())
	}
	for range 2 {
		call := <-observed
		if got := call.metadata.Get(sandboxapi.RuntimeProxyIncarnationUIDMetadataKey); !slices.Equal(got, []string{"test-pod-uid"}) {
			t.Fatalf("Quiesce incarnation headers = %v, want the original Pod UID", got)
		}
		if got := call.metadata.Get("authorization"); !slices.Equal(got, []string{"Bearer test-controller-token"}) {
			t.Fatalf("Quiesce authorization = %v", got)
		}
		if !call.request.GetIfIdleSince().AsTime().Equal(idleSince) {
			t.Fatalf("retry changed the idle fence: %v", call.request)
		}
	}
}

func TestGatewayQuiescerDoesNotRetryRefusalOrUnfinishedSeal(t *testing.T) {
	for _, test := range []struct {
		name  string
		code  codes.Code
		stage sessionpb.QuiesceStage
	}{
		{name: "incarnation changed", code: codes.Aborted},
		{name: "caller denied", code: codes.PermissionDenied},
		{name: "still draining", stage: sessionpb.QuiesceStage_QUIESCE_STAGE_DRAINING},
		{name: "still flushing", stage: sessionpb.QuiesceStage_QUIESCE_STAGE_FLUSHING},
	} {
		t.Run(test.name, func(t *testing.T) {
			var calls atomic.Int32
			quiescer, sandbox := newQuiescerTest(t, func(context.Context, *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error) {
				calls.Add(1)
				if test.code != codes.OK {
					return nil, status.Error(test.code, "test refusal")
				}
				return &sessionpb.QuiesceResponse{Stage: test.stage}, nil
			})
			ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
			defer cancel()
			result, err := quiescer.Quiesce(ctx, sandbox, nil)
			if status.Code(err) != test.code || err == nil && !result.Quiescing {
				t.Fatalf("Quiesce result=%v err=%v, want %v", result, err, test.code)
			}
			if calls.Load() != 1 {
				t.Fatalf("Quiesce calls = %d, want 1", calls.Load())
			}
		})
	}
}

func TestGatewayQuiescerStopsRetryingWhenCallerCancels(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	var calls atomic.Int32
	quiescer, sandbox := newQuiescerTest(t, func(context.Context, *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error) {
		calls.Add(1)
		cancel()
		return nil, status.Error(codes.Unavailable, "test caller canceled during route loss")
	})
	_, err := quiescer.Quiesce(ctx, sandbox, nil)
	if !errors.Is(err, context.Canceled) && status.Code(err) != codes.Canceled {
		t.Fatalf("Quiesce after cancellation = %v, want cancellation", err)
	}
	if calls.Load() != 1 {
		t.Fatalf("Quiesce calls = %d, want 1", calls.Load())
	}
}

func TestGatewayQuiescerBoundsUnavailableRetries(t *testing.T) {
	var calls atomic.Int32
	quiescer, sandbox := newQuiescerTest(t, func(context.Context, *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error) {
		calls.Add(1)
		return nil, status.Error(codes.Unavailable, "test route remains unavailable")
	})
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	_, err := quiescer.Quiesce(ctx, sandbox, nil)
	if status.Code(err) != codes.Unavailable || ctx.Err() != nil {
		t.Fatalf("Quiesce = %v, want bounded Unavailable before caller cancellation", err)
	}
	if calls.Load() != 3 {
		t.Fatalf("Quiesce calls = %d, want 3", calls.Load())
	}
}

func TestQuiesceResultTreatsOnlyQuiescedAsFinal(t *testing.T) {
	stageResponse := func(stage sessionpb.QuiesceStage) *sessionpb.QuiesceResponse {
		return &sessionpb.QuiesceResponse{Stage: stage}
	}

	tests := []struct {
		name      string
		response  *sessionpb.QuiesceResponse
		quiescing bool
	}{
		{
			name:      "draining",
			response:  stageResponse(sessionpb.QuiesceStage_QUIESCE_STAGE_DRAINING),
			quiescing: true,
		},
		{
			name:      "flushing",
			response:  stageResponse(sessionpb.QuiesceStage_QUIESCE_STAGE_FLUSHING),
			quiescing: true,
		},
		{
			name:     "quiesced",
			response: stageResponse(sessionpb.QuiesceStage_QUIESCE_STAGE_QUIESCED),
		},
		{
			name:      "unknown stage",
			response:  stageResponse(sessionpb.QuiesceStage(99)),
			quiescing: true,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := quiesceResult(test.response).Quiescing; got != test.quiescing {
				t.Errorf("quiescing = %t, want %t", got, test.quiescing)
			}
		})
	}
}

func TestGatewayQuiescerUsesControllerSVIDWithoutBearer(t *testing.T) {
	ca := peertlstesting.NewCA(t)
	serverConfig := ca.Issue(t, peertls.SessionDispatcherURI)
	credentials, err := serverConfig.ServerCredentials(peertls.SandboxControllerURI)
	if err != nil {
		t.Fatal(err)
	}
	seen := make(chan metadata.MD, 1)
	quiescer, sandbox := newQuiescerTest(t, func(ctx context.Context, _ *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error) {
		uri, err := peertls.PeerIdentity(ctx)
		if err != nil || uri != peertls.SandboxControllerURI {
			t.Errorf("controller peer = %q, %v", uri, err)
		}
		md, _ := metadata.FromIncomingContext(ctx)
		seen <- md
		return &sessionpb.QuiesceResponse{Stage: sessionpb.QuiesceStage_QUIESCE_STAGE_QUIESCED}, nil
	}, grpc.Creds(credentials))
	leaf := ca.Issue(t, peertls.SandboxControllerURI)
	quiescer.TokenFile = ""
	quiescer.TLS = &peertls.ClientTLS{
		CAFile: ca.RootFile, ServerURI: peertls.SessionDispatcherURI,
		CertFile: leaf.CertFile, KeyFile: leaf.KeyFile, ClientURI: peertls.SandboxControllerURI,
	}
	if _, err := quiescer.Quiesce(t.Context(), sandbox, nil); err != nil {
		t.Fatal(err)
	}
	if md := <-seen; len(md.Get("authorization")) != 0 {
		t.Fatal("SVID quiesce sent bearer metadata")
	}
}
