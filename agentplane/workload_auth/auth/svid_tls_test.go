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
	authlistener "github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/resolver"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestSVIDListenerRefusesAdversarialLeavesBeforeHandler(t *testing.T) {
	for _, kind := range []string{"valid", "forged", "expired", "server-only"} {
		t.Run(kind, func(t *testing.T) {
			objects, sandbox, leaf := svidFixture(t)
			svids, err := auth.NewSVIDResolver(objects, []string{sandbox.Namespace}, nil)
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
			methods := auth.Methods{healthpb.Health_Check_FullMethodName: auth.AnySubject(svids)}
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

// Consolidating a listener must not widen the token writer policy to every Pod.
func TestSharedListenerPreservesBearerWriterAllowlist(t *testing.T) {
	objects, sandbox, leaf := svidFixture(t)
	svids, err := auth.NewSVIDResolver(objects, []string{sandbox.Namespace}, nil)
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := auth.NewResolver(api.TokenReviews(), testAudience, []string{testNamespace, sandbox.Namespace})
	if err != nil {
		t.Fatal(err)
	}
	ca := transporttesting.NewCA(t)
	serverFiles := ca.Issue(t, environment_runtime_transport.HistoryIngressURI)
	config := authlistener.Config{ListenAddress: ":443", TLS: &authlistener.TLS{CertFile: serverFiles.CertFile, KeyFile: serverFiles.KeyFile}, Auth: []authlistener.Authentication{
		&authlistener.SPIFFEAuth{CAFile: serverFiles.CAFile},
		&authlistener.KubernetesBearerAuth{Audiences: []string{testAudience}},
	}}
	resolver, err = resolver.WithListenerAuthentication(config, svids)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := config.ServerCredentials(environment_runtime_transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{healthpb.Health_Check_FullMethodName: auth.SVIDOrAccounts(resolver, []auth.Account{app})}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(serverCredentials))...)
	callers := make(chan auth.Principal, 8)
	healthpb.RegisterHealthServer(server, callerHealth{callers: callers})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	_, allowedToken := workload(t, app)
	_, otherToken := workload(t, auth.Account{Namespace: testNamespace, Name: "other-writer"})
	runtime := ca.Issue(t, leaf.URIs[0].String())
	svidTLS, err := runtime.ClientTLSConfig(environment_runtime_transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	backendTLS, err := (environment_runtime_transport.ClientTLS{CAFile: serverFiles.CAFile, ServerURI: environment_runtime_transport.HistoryIngressURI}).TLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name, token string
		tls         *tls.Config
		admitted    bool
	}{
		{"SVID", "", svidTLS, true},
		{"sampler bearer", allowedToken, backendTLS, true},
		{"other bearer", otherToken, backendTLS, false},
		{"ambiguous", allowedToken, svidTLS, false},
		{"absent", "", backendTLS, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(credentials.NewTLS(test.tls)), grpc.WithNoProxy())
			if err != nil {
				t.Fatal(err)
			}
			defer connection.Close()
			ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
			defer cancel()
			if test.token != "" {
				ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer "+test.token))
			}
			_, err = healthpb.NewHealthClient(connection).Check(ctx, &healthpb.HealthCheckRequest{})
			if test.admitted != (err == nil) {
				t.Fatalf("admitted=%v, error=%v", test.admitted, err)
			}
			if test.admitted {
				<-callers
			} else if len(callers) != 0 {
				t.Fatal("refused caller reached handler")
			}
		})
	}
}

const testPolicyNamespace = "test-policies"

type admissionHealth struct {
	healthpb.UnimplementedHealthServer
	admissions chan auth.Admission
}

func (h admissionHealth) Check(ctx context.Context, _ *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return nil, err
	}
	h.admissions <- admission
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
}

// An AnySubject or ReviewedServiceAccountOrIdentity method admits an Identity caller as an
// IdentityAdmission and a ServiceAccount caller as before; a method that has not opted in refuses the
// Identity caller.
func TestIdentityAdmittingRulesAdmitIdentityCallers(t *testing.T) {
	for _, rule := range []struct {
		name  string
		admit func(*auth.Resolver) auth.Rule
	}{
		{"AnySubject", func(r *auth.Resolver) auth.Rule { return auth.AnySubject(r) }},
		{"ReviewedServiceAccountOrIdentity", auth.ReviewedServiceAccountOrIdentity},
	} {
		t.Run(rule.name, func(t *testing.T) { testIdentityAdmittingRule(t, rule.admit) })
	}
}

func testIdentityAdmittingRule(t *testing.T, admit func(*auth.Resolver) auth.Rule) {
	accountWorkload, _ := svidWorkload(t)
	identityWorkload := workloadauthtesting.NewIdentityWorkload(t, api, "svid-"+uuid.NewString(), "sandbox-"+uuid.NewString()[:8], testPolicyNamespace)
	namespaces := []string{accountWorkload.Sandbox.Namespace, identityWorkload.Sandbox.Namespace}
	svids, err := auth.NewSVIDResolver(accountWorkload.Objects, namespaces, &resolver.Identities{PolicyNamespace: testPolicyNamespace})
	if err != nil {
		t.Fatal(err)
	}
	ca := transporttesting.NewCA(t)
	serverFiles := ca.Issue(t, environment_runtime_transport.HistoryIngressURI)
	config := authlistener.Config{ListenAddress: ":443", TLS: &authlistener.TLS{CertFile: serverFiles.CertFile, KeyFile: serverFiles.KeyFile}, Auth: []authlistener.Authentication{
		&authlistener.SPIFFEAuth{CAFile: serverFiles.CAFile},
	}}
	resolves, err := newResolver(t, api.TokenReviews()).WithListenerAuthentication(config, svids)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := config.ServerCredentials(environment_runtime_transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{
		healthpb.Health_Check_FullMethodName: admit(resolves),
		healthpb.Health_List_FullMethodName:  auth.ReviewedServiceAccount(resolves),
	}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(serverCredentials))...)
	admissions := make(chan auth.Admission, 4)
	healthpb.RegisterHealthServer(server, admissionHealth{admissions: admissions})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	client := func(t *testing.T, uri string) healthpb.HealthClient {
		t.Helper()
		clientTLS, err := ca.Issue(t, uri).ClientTLSConfig(environment_runtime_transport.HistoryIngressURI)
		if err != nil {
			t.Fatal(err)
		}
		connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)), grpc.WithNoProxy())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = connection.Close() })
		return healthpb.NewHealthClient(connection)
	}

	identityClient := client(t, identityWorkload.URI)
	if _, err := identityClient.Check(t.Context(), &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatal(err)
	}
	if got := <-admissions; got != (auth.IdentityAdmission{Caller: identityWorkload.Subject}) {
		t.Fatalf("Identity caller admitted as %#v, want %#v", got, identityWorkload.Subject)
	}
	if _, err := identityClient.List(t.Context(), &healthpb.HealthListRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("ServiceAccount-only method admitted an Identity caller: %v", err)
	}

	if _, err := client(t, accountWorkload.URI).Check(t.Context(), &healthpb.HealthCheckRequest{}); err != nil {
		t.Fatal(err)
	}
	direct, ok := (<-admissions).(auth.DirectAdmission)
	if !ok || direct.Pod == nil || *direct.Pod != accountWorkload.Principal {
		t.Fatalf("ServiceAccount caller admitted as %#v, want %#v", direct, accountWorkload.Principal)
	}
	if len(admissions) != 0 {
		t.Fatal("a refused call reached the handler")
	}
}
