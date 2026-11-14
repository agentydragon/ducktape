package auth_test

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type unusedSubjectResolver struct{ calls atomic.Int32 }

func (r *unusedSubjectResolver) ResolveSubject(context.Context) (auth.Subject, error) {
	r.calls.Add(1)
	return nil, fmt.Errorf("unexpected workload resolver")
}

func TestServiceCertificateAdmissionAndDelegation(t *testing.T) {
	fallback := new(unusedSubjectResolver)
	identity := auth.Identity{Name: "owner", UID: "owner-uid"}
	identities := identityUIDReaderFunc(func(_ context.Context, name string) (string, bool, error) {
		return identity.UID, name == identity.Name, nil
	})
	rule, err := auth.AnySubject(fallback).AllowServiceCallers([]string{peertls.ActionsURI})
	if err != nil {
		t.Fatal(err)
	}
	rule, err = rule.AllowServiceDelegationFrom([]auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}}, identities)
	if err != nil {
		t.Fatal(err)
	}
	ca := transporttesting.NewCA(t)
	serverFiles := ca.Issue(t, peertls.HistoryIngressURI)
	serverTLS, err := serverFiles.WorkloadServerTLSConfigWithServices(peertls.HistoryIngressURI, true)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{healthpb.Health_Check_FullMethodName: rule}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	calls := make(chan auth.Admission, 16)
	healthpb.RegisterHealthServer(server, serviceAdmissionHealth{admissions: calls})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	valid := ca.Issue(t, peertls.ActionsURI)
	actorTLS, err := valid.ClientTLSConfig(peertls.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	backendTLS, err := (peertls.ClientTLS{CAFile: ca.RootFile, ServerURI: peertls.HistoryIngressURI}).TLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	envelope := &delegationpb.Delegation{EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: identity.Name, Uid: identity.UID}}, Provenance: &delegationpb.Delegation_Session{Session: &delegationpb.SessionProvenance{SessionId: "a57a540c-ed82-4b85-b914-73746dba4bad"}}}
	encoded, err := auth.MarshalDelegation(envelope)
	if err != nil {
		t.Fatal(err)
	}
	wrongIdentity := &delegationpb.Delegation{EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: identity.Name, Uid: "replaced-uid"}}, Provenance: envelope.Provenance}
	wrongEncoded, err := auth.MarshalDelegation(wrongIdentity)
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name      string
		tls       *tls.Config
		md        metadata.MD
		code      codes.Code
		delegated bool
	}{
		{name: "direct exact service", tls: actorTLS, code: codes.OK},
		{name: "delegated effective Identity", tls: actorTLS, md: metadata.Pairs(auth.DelegationMetadataKey, string(encoded)), code: codes.OK, delegated: true},
		{name: "no certificate envelope", tls: backendTLS, md: metadata.Pairs(auth.DelegationMetadataKey, string(encoded)), code: codes.Unauthenticated},
		{name: "bearer cannot supplement certificate", tls: actorTLS, md: metadata.Pairs("authorization", "Bearer valid-token"), code: codes.Unauthenticated},
		{name: "legacy delegated token cannot supplement certificate", tls: actorTLS, md: metadata.Pairs("authorization", "Agentplane-Delegated valid-token", auth.DelegationMetadataKey, string(encoded)), code: codes.Unauthenticated},
		{name: "duplicate envelope", tls: actorTLS, md: metadata.Pairs(auth.DelegationMetadataKey, string(encoded), auth.DelegationMetadataKey, string(encoded)), code: codes.Unauthenticated},
		{name: "malformed envelope", tls: actorTLS, md: metadata.Pairs(auth.DelegationMetadataKey, "broken"), code: codes.Unauthenticated},
		{name: "replaced effective Identity", tls: actorTLS, md: metadata.Pairs(auth.DelegationMetadataKey, string(wrongEncoded)), code: codes.PermissionDenied},
	} {
		t.Run(tc.name, func(t *testing.T) {
			conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(tc.tls)))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
			defer cancel()
			ctx = metadata.NewOutgoingContext(ctx, tc.md)
			_, err = healthpb.NewHealthClient(conn).Check(ctx, &healthpb.HealthCheckRequest{})
			if status.Code(err) != tc.code {
				t.Fatalf("got %v, want %s", err, tc.code)
			}
			if tc.code == codes.OK {
				admission := <-calls
				if tc.delegated {
					delegated, ok := admission.(auth.DelegatedServiceIdentityAdmission)
					if !ok || delegated.Actor.SPIFFEID != peertls.ActionsURI || delegated.EffectiveCaller != identity {
						t.Fatalf("delegation=%#v", admission)
					}
				} else if admission != (auth.ServiceAdmission{Caller: auth.ServicePrincipal{SPIFFEID: peertls.ActionsURI}}) {
					t.Fatalf("direct=%#v", admission)
				}
			} else if len(calls) != 0 {
				t.Fatal("denied call reached handler")
			}
		})
	}
	for _, kind := range []string{"wrong service", "untrusted issuer", "server-only", "expired"} {
		t.Run(kind, func(t *testing.T) {
			presented := valid
			expected := codes.Unavailable
			switch kind {
			case "wrong service":
				presented = ca.Issue(t, peertls.NotificationsURI)
				expected = codes.PermissionDenied
			case "untrusted issuer":
				presented = transporttesting.NewCA(t).Issue(t, peertls.ActionsURI)
			case "server-only":
				presented = ca.IssueWithUsage(t, peertls.ActionsURI, x509.ExtKeyUsageServerAuth)
			case "expired":
				presented = ca.IssueWithValidity(t, peertls.ActionsURI, time.Now().Add(-time.Hour), time.Now().Add(-time.Minute), x509.ExtKeyUsageClientAuth)
			}
			pair, err := tls.LoadX509KeyPair(presented.CertFile, presented.KeyFile)
			if err != nil {
				t.Fatal(err)
			}
			clientTLS := actorTLS.Clone()
			clientTLS.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) { return &pair, nil }
			conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			ctx, cancel := context.WithTimeout(t.Context(), 2*time.Second)
			defer cancel()
			ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs(auth.DelegationMetadataKey, string(encoded)))
			_, err = healthpb.NewHealthClient(conn).Check(ctx, &healthpb.HealthCheckRequest{})
			if status.Code(err) != expected || len(calls) != 0 {
				t.Fatalf("call=%v, handler calls=%d", err, len(calls))
			}
		})
	}
	if fallback.calls.Load() != 0 {
		t.Fatalf("workload resolver calls=%d; no service attempt may fall back", fallback.calls.Load())
	}
}

type serviceAdmissionHealth struct {
	healthpb.UnimplementedHealthServer
	admissions chan auth.Admission
}

func (h serviceAdmissionHealth) Check(ctx context.Context, _ *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return nil, err
	}
	if _, err := auth.Caller(ctx); err == nil {
		return nil, fmt.Errorf("service acquired legacy account proof")
	}
	if _, err := auth.EffectiveCaller(ctx); err == nil {
		return nil, fmt.Errorf("service acquired effective account authority")
	}
	h.admissions <- admission
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
}
