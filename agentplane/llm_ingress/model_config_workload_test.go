package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"net"
	"net/http"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	modelconfigpb "github.com/agentydragon/ducktape/agentplane/llm_ingress/v1"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/status"
)

func TestWorkloadModelConfigGRPCUsesVerifiedTLSHTTPPeer(t *testing.T) {
	workload := workloadauthtesting.NewWorkload(t, api, testNamespace, "llm-model-config-grpc")
	ca := transporttesting.NewCA(t)
	serverCredentials := ca.Issue(t, environment_runtime_transport.LLMIngressURI)
	clientCredentials := ca.IssueWithUsage(t, workload.URI, x509.ExtKeyUsageClientAuth)
	resolver, err := auth.NewSVIDResolver(workload.Objects, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	service := &countedModelConfigService{delegate: &modelConfigService{
		models: map[string]modelConfig{"configured": {Model: "configured", TotalContextBudgetTokens: 8192}},
	}}
	grpcServer := newModelConfigServer(resolver, service)
	address := serveWorkloadModelConfigHTTP2(t, grpcServer, serverCredentials)
	client := newWorkloadModelConfigGRPCClient(t, address, clientCredentials, clientCredentials)

	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	response, err := modelconfigpb.NewModelConfigServiceClient(client).GetModelConfig(
		ctx,
		&modelconfigpb.GetModelConfigRequest{Model: "configured"},
	)
	if err != nil || response.GetTotalContextBudgetTokens() != 8192 {
		t.Fatalf("SVID-authenticated model config = %v, %v; want configured budget", response, err)
	}
	if got := service.calls.Load(); got != 1 {
		t.Fatalf("ModelConfig handler calls = %d, want one after actual TLS peer admission", got)
	}
}

func TestWorkloadModelConfigGRPCRejectsUntrustedExpiredAndUnacceptedLeavesBeforeHandler(t *testing.T) {
	for _, test := range []struct {
		name                string
		workloadName        string
		prepare             func(t *testing.T, ca *transporttesting.CA, workload workloadauthtesting.Workload) environment_runtime_transport.Config
		mutate              func(t *testing.T, workload workloadauthtesting.Workload)
		wantUnauthenticated bool
	}{
		{
			name:         "untrusted leaf",
			workloadName: "llm-model-config-untrusted",
			prepare: func(t *testing.T, _ *transporttesting.CA, workload workloadauthtesting.Workload) environment_runtime_transport.Config {
				untrustedCA := transporttesting.NewCA(t)
				return untrustedCA.IssueWithUsage(t, workload.URI, x509.ExtKeyUsageClientAuth)
			},
		},
		{
			name:         "expired leaf",
			workloadName: "llm-model-config-expired",
			prepare: func(t *testing.T, ca *transporttesting.CA, workload workloadauthtesting.Workload) environment_runtime_transport.Config {
				now := time.Now().UTC()
				return ca.IssueWithValidity(
					t,
					workload.URI,
					now.Add(-2*time.Hour),
					now.Add(-time.Minute),
					x509.ExtKeyUsageClientAuth,
				)
			},
		},
		{
			name:                "rotated credential",
			workloadName:        "llm-model-config-unaccepted",
			wantUnauthenticated: true,
			prepare: func(t *testing.T, ca *transporttesting.CA, workload workloadauthtesting.Workload) environment_runtime_transport.Config {
				return ca.IssueWithUsage(t, workload.URI, x509.ExtKeyUsageClientAuth)
			},
			mutate: func(t *testing.T, workload workloadauthtesting.Workload) {
				workload.Issue(t, "ffffffffffffffffffffffffffffffff")
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			workload := workloadauthtesting.NewWorkload(t, api, testNamespace, test.workloadName)
			ca := transporttesting.NewCA(t)
			serverCredentials := ca.Issue(t, environment_runtime_transport.LLMIngressURI)
			trustedClientCredentials := ca.IssueWithUsage(t, workload.URI, x509.ExtKeyUsageClientAuth)
			invalidClientCredentials := test.prepare(t, ca, workload)
			resolver, err := auth.NewSVIDResolver(workload.Objects, []string{testNamespace})
			if err != nil {
				t.Fatal(err)
			}
			service := &countedModelConfigService{delegate: &modelConfigService{models: map[string]modelConfig{
				"configured": {Model: "configured", TotalContextBudgetTokens: 8192},
			}}}
			grpcServer := newModelConfigServer(resolver, service)
			address := serveWorkloadModelConfigHTTP2(t, grpcServer, serverCredentials)
			client := newWorkloadModelConfigGRPCClient(t, address, trustedClientCredentials, invalidClientCredentials)
			if test.mutate != nil {
				test.mutate(t, workload)
			}

			ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
			defer cancel()
			_, err = modelconfigpb.NewModelConfigServiceClient(client).GetModelConfig(
				ctx,
				&modelconfigpb.GetModelConfigRequest{Model: "configured"},
			)
			if err == nil {
				t.Fatal("invalid workload leaf admitted")
			}
			if test.wantUnauthenticated && status.Code(err) != codes.Unauthenticated {
				t.Fatalf("unaccepted Sandbox credential status = %v, want unauthenticated", err)
			}
			if got := service.calls.Load(); got != 0 {
				t.Fatalf("ModelConfig handler calls = %d, want zero for invalid workload leaf", got)
			}
		})
	}
}

type countedModelConfigService struct {
	modelconfigpb.UnimplementedModelConfigServiceServer
	delegate modelconfigpb.ModelConfigServiceServer
	calls    atomic.Int32
}

func (s *countedModelConfigService) GetModelConfig(
	ctx context.Context,
	request *modelconfigpb.GetModelConfigRequest,
) (*modelconfigpb.GetModelConfigResponse, error) {
	s.calls.Add(1)
	return s.delegate.GetModelConfig(ctx, request)
}

func serveWorkloadModelConfigHTTP2(
	t *testing.T,
	grpcServer *grpc.Server,
	serverCredentials environment_runtime_transport.Config,
) string {
	t.Helper()
	tlsConfig, err := serverCredentials.WorkloadServerTLSConfig(environment_runtime_transport.LLMIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	tlsConfig.NextProtos = []string{"h2"}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	httpServer := &http.Server{
		Handler:   rpcHandler(grpcServer, http.NotFoundHandler()),
		TLSConfig: tlsConfig,
	}
	go func() {
		if err := httpServer.ServeTLS(listener, "", ""); err != nil && err != http.ErrServerClosed {
			t.Errorf("serve workload ModelConfig HTTP/2 listener: %v", err)
		}
	}()
	t.Cleanup(func() {
		_ = httpServer.Close()
		grpcServer.Stop()
	})
	return listener.Addr().String()
}

func newWorkloadModelConfigGRPCClient(
	t *testing.T,
	address string,
	trustedCredentials environment_runtime_transport.Config,
	clientLeaf environment_runtime_transport.Config,
) *grpc.ClientConn {
	t.Helper()
	tlsConfig, err := trustedCredentials.ClientTLSConfig(environment_runtime_transport.LLMIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	clientCertificate, err := tls.LoadX509KeyPair(clientLeaf.CertFile, clientLeaf.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	tlsConfig = tlsConfig.Clone()
	tlsConfig.Certificates = nil
	tlsConfig.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
		return &clientCertificate, nil
	}
	tlsConfig.NextProtos = []string{"h2"}
	client, err := grpc.NewClient(address, grpc.WithTransportCredentials(credentials.NewTLS(tlsConfig)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	return client
}
