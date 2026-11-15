package rpc

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"os"
	"testing"

	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"
)

func serviceContext(t *testing.T, caller string) context.Context {
	t.Helper()
	ca := transporttesting.NewCA(t)
	files := ca.Issue(t, caller)
	certificate, err := tls.LoadX509KeyPair(files.CertFile, files.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(certificate.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	roots := x509.NewCertPool()
	raw, err := os.ReadFile(ca.RootFile)
	if err != nil || !roots.AppendCertsFromPEM(raw) {
		t.Fatalf("roots: %v", err)
	}
	chains, err := leaf.Verify(x509.VerifyOptions{Roots: roots, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}})
	if err != nil {
		t.Fatal(err)
	}
	return peer.NewContext(t.Context(), &peer.Peer{AuthInfo: credentials.TLSInfo{State: tls.ConnectionState{
		PeerCertificates: []*x509.Certificate{leaf}, VerifiedChains: chains,
	}}})
}

func fixtureSettings() samplersettings.Settings {
	platformInstructions := "Fixture Agentplane guidance."
	return samplersettings.Settings{
		Models:      map[string]samplersettings.ModelConfig{"test-model": {Connection: "provider", ProviderModel: "test-model", API: protocol.APIStyleResponses, Responses: responses.Config{Dialect: responses.DialectLite}, ExecEncoding: protocol.ExecEncodingFreeform, ReasoningEfforts: []string{"", "high"}}},
		Connections: map[string]samplersettings.ConnectionConfig{"provider": {BaseURL: "https://model.invalid", Auth: samplersettings.AuthConfig{Type: samplersettings.AuthTypeBearer, Credential: "test-credential"}}},
		DatabaseURL: "postgresql://fixture.invalid/sampler", ListenTLS: &peertls.ServerTLSListener{Listen: "127.0.0.1:8083", CertFile: "/fixture/tls.crt", KeyFile: "/fixture/tls.key", ClientCAFile: "/fixture/ca.crt"}, ServiceCallers: []string{peertls.SessionsURI}, PlatformInstructions: &platformInstructions, CodeModeHost: "/fixture/host", ExecutorCodex: "/fixture/codex", Logs: "/fixture/logs", DirectExecutorTarget: "127.0.0.1:4501", DirectExecutorInsecureLocaltest: true, MaxLiveSessions: 2, RPCTimeoutSeconds: 10, LeaseSeconds: 60,
	}
}
