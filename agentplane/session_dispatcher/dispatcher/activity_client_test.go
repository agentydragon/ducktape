package dispatcher

import (
	"net"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/harness_host"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type activityWireHost struct {
	harnesshostpb.UnimplementedHarnessHostServer
	version []string
	sandbox SandboxSnapshot
	t       *testing.T
}

func (h *activityWireHost) WatchSessions(_ *harnesshostpb.WatchSessionsRequest, stream grpc.ServerStreamingServer[harnesshostpb.WatchSessionsResponse]) error {
	md, _ := metadata.FromIncomingContext(stream.Context())
	for key, want := range map[string]string{sandboxapi.RuntimeProxySandboxMetadataKey: h.sandbox.Name, sandboxapi.RuntimeProxySandboxUIDMetadataKey: h.sandbox.UID, sandboxapi.RuntimeProxySandboxNamespaceMetadataKey: h.sandbox.Namespace} {
		if got := md.Get(key); len(got) != 1 || got[0] != want {
			h.t.Errorf("routing %s=%v, want %s", key, got, want)
		}
	}
	header := metadata.MD{}
	if h.version != nil {
		header[harnesshost.SessionVersionMetadataKey] = h.version
	}
	if err := stream.SendHeader(header); err != nil {
		return err
	}
	// Old WatchSessions used the same field-1 envelope as this one-item legacy list.
	if len(h.version) != 1 || h.version[0] != harnesshost.SessionVersion {
		return stream.SendMsg(&sessionpb.ListSessionsResponse{Sessions: []*sessionpb.SessionSummary{{SessionId: "old-session", Spec: &sessionpb.SessionSpec{}}}})
	}
	return stream.Send(&harnesshostpb.WatchSessionsResponse{Update: &harnesshostpb.WatchSessionsResponse_CaughtUp{CaughtUp: &harnesshostpb.CaughtUp{}}})
}

func TestActivityClientGuardsWireVersionAndRoutes(t *testing.T) {
	for _, test := range []struct {
		name    string
		version []string
		allowed bool
	}{
		{"old Runtime", nil, false}, {"unsupported", []string{"2"}, false}, {"duplicate", []string{"1", "1"}, false}, {"current", []string{"1"}, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			ca := peertlstesting.NewCA(t)
			serving := ca.Issue(t, peertls.SandboxControllerURI)
			tlsConfig, err := serving.WorkloadServerTLSConfigWithServices(peertls.SandboxControllerURI, true)
			if err != nil {
				t.Fatal(err)
			}
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			server := grpc.NewServer(grpc.Creds(credentials.NewTLS(tlsConfig)))
			sandbox := SandboxSnapshot{Name: "wire-guard", Namespace: "test", UID: "sandbox-uid"}
			harnesshostpb.RegisterHarnessHostServer(server, &activityWireHost{version: test.version, sandbox: sandbox, t: t})
			go server.Serve(listener)
			t.Cleanup(server.Stop)
			caller := ca.Issue(t, peertls.SessionsURI)
			client := &RuntimeProxyClient{Namespace: "test", Timeout: 5 * time.Second, TargetTemplate: listener.Addr().String(), GatewayTLS: &peertls.ClientTLS{CAFile: ca.RootFile, ServerURI: peertls.SandboxControllerURI, CertFile: caller.CertFile, KeyFile: caller.KeyFile, ClientURI: peertls.SessionsURI}}
			applied := 0
			sentinel := status.Error(codes.Canceled, "test received guarded frame")
			err = client.WatchActivity(t.Context(), sandbox, func(*harnesshostpb.WatchSessionsResponse) error { applied++; return sentinel })
			if test.allowed {
				if applied != 1 || err != sentinel {
					t.Fatalf("current stream applied=%d err=%v", applied, err)
				}
			} else if applied != 0 || status.Code(err) != codes.FailedPrecondition {
				t.Fatalf("unguarded old wire applied=%d err=%v", applied, err)
			}
		})
	}
}
