package auth_test

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"net"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestSVIDListenerRefusesAdversarialLeavesBeforeHandler(t *testing.T) {
	for _, kind := range []string{"valid", "forged", "expired", "server-only"} {
		t.Run(kind, func(t *testing.T) {
			objects, sandbox, leaf := svidFixture(t)
			svids, err := auth.NewSVIDResolver(objects, []string{sandbox.Namespace})
			if err != nil {
				t.Fatal(err)
			}
			tokens, err := auth.NewResolver(unusedSVIDReviews{t}, "backend-only", []string{sandbox.Namespace})
			if err != nil {
				t.Fatal(err)
			}
			ca := transporttesting.NewCA(t)
			serverConfig := ca.Issue(t, environment_runtime_transport.HistoryIngressURI)
			serverTLS, err := serverConfig.WorkloadServerTLSConfig(environment_runtime_transport.HistoryIngressURI)
			if err != nil {
				t.Fatal(err)
			}
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			methods := auth.Methods{healthpb.Health_Check_FullMethodName: auth.AnyAccount(tokens.WithSVID(svids))}
			server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
			handler := &svidHealth{expected: auth.Principal{Account: auth.Account{Namespace: sandbox.Namespace, Name: sandbox.Name}, ServiceAccountUID: accountUID(t, objects, sandbox), PodName: sandbox.Status.Incarnation.Name, PodUID: sandbox.Status.Incarnation.UID}}
			healthpb.RegisterHealthServer(server, handler)
			go func() { _ = server.Serve(listener) }()
			t.Cleanup(server.Stop)
			runtimeURI := leaf.URIs[0].String()
			valid := ca.Issue(t, runtimeURI)
			clientTLS, err := valid.ClientTLSConfig(environment_runtime_transport.HistoryIngressURI)
			if err != nil {
				t.Fatal(err)
			}
			presented := valid
			switch kind {
			case "forged":
				presented = transporttesting.NewCA(t).Issue(t, runtimeURI)
			case "expired":
				presented = ca.IssueWithValidity(t, runtimeURI, time.Now().Add(-time.Hour), time.Now().Add(-time.Minute), x509.ExtKeyUsageClientAuth)
			case "server-only":
				presented = ca.IssueWithUsage(t, runtimeURI, x509.ExtKeyUsageServerAuth)
			}
			pair, err := tls.LoadX509KeyPair(presented.CertFile, presented.KeyFile)
			if err != nil {
				t.Fatal(err)
			}
			// Only the certificate presented to the server changes. Destination
			// verification stays enabled with the transport's pinned service URI.
			clientTLS.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) { return &pair, nil }
			connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)), grpc.WithNoProxy())
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = connection.Close() })
			ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
			defer cancel()
			ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("x-agentplane-subject", "test/attacker", "authorization", "Bearer forged"))
			_, err = healthpb.NewHealthClient(connection).Check(ctx, &healthpb.HealthCheckRequest{})
			if kind == "valid" {
				if err != nil || handler.calls.Load() != 1 {
					t.Fatalf("valid call: error=%v calls=%d", err, handler.calls.Load())
				}
			} else if err == nil || handler.calls.Load() != 0 {
				t.Fatalf("%s leaf: error=%v calls=%d", kind, err, handler.calls.Load())
			}
		})
	}
}

type unusedSVIDReviews struct{ t *testing.T }

func (r unusedSVIDReviews) Create(context.Context, *authenticationv1.TokenReview, metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	r.t.Error("SVID authentication reached TokenReview")
	return &authenticationv1.TokenReview{}, nil
}

type svidHealth struct {
	healthpb.UnimplementedHealthServer
	calls    atomic.Int32
	expected auth.Principal
}

func (s *svidHealth) Check(ctx context.Context, _ *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	principal, err := auth.Caller(ctx)
	if err != nil {
		return nil, err
	}
	if principal != s.expected {
		return nil, status.Error(codes.PermissionDenied, "unexpected subject")
	}
	s.calls.Add(1)
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
}
