package auth_test

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	authlistener "github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestSVIDListenerRefusesAdversarialLeavesBeforeHandler(t *testing.T) {
	for _, kind := range []string{"valid", "forged", "expired", "server-only", "mixed bearer", "mixed delegation"} {
		t.Run(kind, func(t *testing.T) {
			workload, leaf := svidFixture(t)
			svids, err := auth.NewSVIDResolver(workload.Objects, []string{workload.Sandbox.Namespace}, &resolver.Identities{PolicyNamespace: testPolicyNamespace})
			if err != nil {
				t.Fatal(err)
			}
			ca := transporttesting.NewCA(t)
			serverConfig := ca.Issue(t, peertls.HistoryIngressURI)
			serverTLS, err := serverConfig.WorkloadServerTLSConfig(peertls.HistoryIngressURI)
			if err != nil {
				t.Fatal(err)
			}
			methods := auth.Methods{healthpb.Health_Check_FullMethodName: auth.AnySubject(svids)}
			server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
			handler := &svidHealth{expected: auth.IdentityAdmission{Caller: workload.Subject}}
			healthpb.RegisterHealthServer(server, handler)
			address := grpcserver.Serve(t, server)
			runtimeURI := leaf.URIs[0].String()
			valid := ca.Issue(t, runtimeURI)
			clientTLS, err := valid.ClientTLSConfig(peertls.HistoryIngressURI)
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
			connection, err := grpc.NewClient(address, grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)), grpc.WithNoProxy())
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = connection.Close() })
			ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
			defer cancel()
			ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("x-agentplane-subject", "test/attacker"))
			if kind == "mixed bearer" {
				ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer forged")
			}
			if kind == "mixed delegation" {
				ctx = metadata.AppendToOutgoingContext(ctx, auth.DelegationMetadataKey, "")
			}
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

type svidHealth struct {
	healthpb.UnimplementedHealthServer
	calls    atomic.Int32
	expected auth.Admission
}

func (s *svidHealth) Check(ctx context.Context, _ *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return nil, err
	}
	if admission != s.expected {
		return nil, status.Error(codes.PermissionDenied, "unexpected subject")
	}
	s.calls.Add(1)
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
}

// One TLS listener may serve workload Identities and exact service roles without
// turning its workload admission into a service-only method grant.
func TestSharedListenerKeepsWorkloadAndServiceGrantsSeparate(t *testing.T) {
	fixture, _ := svidFixture(t)
	svids, err := auth.NewSVIDResolver(fixture.Objects, []string{fixture.Sandbox.Namespace}, &resolver.Identities{PolicyNamespace: testPolicyNamespace})
	if err != nil {
		t.Fatal(err)
	}
	shared, err := auth.AnySubject(svids).AllowServiceCallers([]string{peertls.CentralSamplerURI})
	if err != nil {
		t.Fatal(err)
	}
	serviceOnly, err := auth.Services([]string{peertls.CentralSamplerURI})
	if err != nil {
		t.Fatal(err)
	}
	ca := transporttesting.NewCA(t)
	serverFiles := ca.Issue(t, peertls.HistoryIngressURI)
	config := authlistener.Config{ListenAddress: ":443", TLS: &authlistener.TLS{CertFile: serverFiles.CertFile, KeyFile: serverFiles.KeyFile}, Auth: []authlistener.Authentication{
		&authlistener.SPIFFEAuth{CAFile: serverFiles.CAFile},
	}}
	serverCredentials, err := config.ServerCredentialsWithServices(peertls.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{
		healthpb.Health_Check_FullMethodName: shared,
		healthpb.Health_List_FullMethodName:  serviceOnly,
	}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(serverCredentials))...)
	admissions := make(chan auth.Admission, 1)
	healthpb.RegisterHealthServer(server, admissionHealth{admissions: admissions})
	address := grpcserver.Serve(t, server)
	workloadAdmission := auth.IdentityAdmission{Caller: fixture.Subject}
	serviceAdmission := auth.ServiceAdmission{Caller: auth.ServicePrincipal{SPIFFEID: peertls.CentralSamplerURI}}
	for _, tc := range []struct {
		name, uri           string
		md                  metadata.MD
		checkCode, listCode codes.Code
		want                auth.Admission
	}{
		{name: "workload Identity", uri: fixture.URI, checkCode: codes.OK, listCode: codes.Unauthenticated, want: workloadAdmission},
		{name: "granted service", uri: peertls.CentralSamplerURI, checkCode: codes.OK, listCode: codes.OK, want: serviceAdmission},
		{name: "ungranted service", uri: peertls.AppURI, checkCode: codes.PermissionDenied, listCode: codes.PermissionDenied},
		{name: "missing certificate", checkCode: codes.Unauthenticated, listCode: codes.Unauthenticated},
		{name: "bearer without certificate", md: metadata.Pairs("authorization", "Bearer test-token"), checkCode: codes.Unauthenticated, listCode: codes.Unauthenticated},
		{name: "workload plus bearer", uri: fixture.URI, md: metadata.Pairs("authorization", "Bearer test-token"), checkCode: codes.Unauthenticated, listCode: codes.Unauthenticated},
		{name: "service plus bearer", uri: peertls.CentralSamplerURI, md: metadata.Pairs("authorization", "Bearer test-token"), checkCode: codes.Unauthenticated, listCode: codes.Unauthenticated},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var clientTLS *tls.Config
			var err error
			if tc.uri == "" {
				clientTLS, err = (peertls.ClientTLS{CAFile: serverFiles.CAFile, ServerURI: peertls.HistoryIngressURI}).TLSConfig()
			} else {
				clientTLS, err = ca.Issue(t, tc.uri).ClientTLSConfig(peertls.HistoryIngressURI)
			}
			if err != nil {
				t.Fatal(err)
			}
			connection, err := grpc.NewClient(address, grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)), grpc.WithNoProxy())
			if err != nil {
				t.Fatal(err)
			}
			defer connection.Close()
			client := healthpb.NewHealthClient(connection)
			for _, call := range []struct {
				name   string
				invoke func(context.Context) error
				code   codes.Code
			}{
				{"shared", func(ctx context.Context) error {
					_, err := client.Check(ctx, &healthpb.HealthCheckRequest{})
					return err
				}, tc.checkCode},
				{"service-only", func(ctx context.Context) error { _, err := client.List(ctx, &healthpb.HealthListRequest{}); return err }, tc.listCode},
			} {
				t.Run(call.name, func(t *testing.T) {
					ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
					defer cancel()
					err := call.invoke(metadata.NewOutgoingContext(ctx, tc.md))
					if status.Code(err) != call.code {
						t.Fatalf("call = %v, want %v", err, call.code)
					}
					if call.code == codes.OK {
						if got := <-admissions; got != tc.want {
							t.Fatalf("admission = %#v, want %#v", got, tc.want)
						}
					} else if len(admissions) != 0 {
						t.Fatal("refused caller reached handler")
					}
				})
			}
		})
	}
}

const testPolicyNamespace = "test-policies"

type admissionHealth struct {
	healthpb.UnimplementedHealthServer
	admissions chan auth.Admission
}

func (h admissionHealth) record(ctx context.Context) error {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return err
	}
	h.admissions <- admission
	return nil
}

func (h admissionHealth) Check(ctx context.Context, _ *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	if err := h.record(ctx); err != nil {
		return nil, err
	}
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
}

func (h admissionHealth) List(ctx context.Context, _ *healthpb.HealthListRequest) (*healthpb.HealthListResponse, error) {
	if err := h.record(ctx); err != nil {
		return nil, err
	}
	return &healthpb.HealthListResponse{}, nil
}
