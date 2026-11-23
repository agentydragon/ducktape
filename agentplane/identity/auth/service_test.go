package auth_test

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type unusedSubjectResolver struct{ calls atomic.Int32 }

func (r *unusedSubjectResolver) ResolveSubject(context.Context) (auth.Subject, error) {
	r.calls.Add(1)
	return nil, fmt.Errorf("unexpected workload resolver")
}

func TestServiceCertificateAdmissionAndDelegation(t *testing.T) {
	for _, serviceOnly := range []bool{false, true} {
		t.Run(fmt.Sprintf("service-only=%t", serviceOnly), func(t *testing.T) {
			testServiceCertificateAdmissionAndDelegation(t, serviceOnly)
		})
	}
}

func testServiceCertificateAdmissionAndDelegation(t *testing.T, serviceOnly bool) {
	t.Helper()
	fallback := new(unusedSubjectResolver)
	identity := auth.Identity{Name: "owner", UID: "owner-uid"}
	identities := identityUIDReaderFunc(func(_ context.Context, name string) (string, bool, error) {
		return identity.UID, name == identity.Name, nil
	})
	rule, err := auth.AnySubject(fallback).AllowServiceCallers([]string{peertls.ActionsURI})
	directCode := codes.OK
	if serviceOnly {
		rule, err = auth.Services(nil)
		directCode = codes.PermissionDenied
	}
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
	methods := auth.Methods{healthpb.Health_Check_FullMethodName: rule}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	calls := make(chan auth.Admission, 16)
	healthpb.RegisterHealthServer(server, serviceAdmissionHealth{admissions: calls})
	address := grpcserver.Serve(t, server)
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
		{name: "direct exact service", tls: actorTLS, code: directCode},
		{name: "delegated effective Identity", tls: actorTLS, md: metadata.Pairs(auth.DelegationMetadataKey, string(encoded)), code: codes.OK, delegated: true},
		{name: "no certificate envelope", tls: backendTLS, md: metadata.Pairs(auth.DelegationMetadataKey, string(encoded)), code: codes.Unauthenticated},
		{name: "bearer cannot supplement certificate", tls: actorTLS, md: metadata.Pairs("authorization", "Bearer valid-token"), code: codes.Unauthenticated},
		{name: "legacy delegated token cannot supplement certificate", tls: actorTLS, md: metadata.Pairs("authorization", "Agentplane-Delegated valid-token", auth.DelegationMetadataKey, string(encoded)), code: codes.Unauthenticated},
		{name: "duplicate envelope", tls: actorTLS, md: metadata.Pairs(auth.DelegationMetadataKey, string(encoded), auth.DelegationMetadataKey, string(encoded)), code: codes.Unauthenticated},
		{name: "malformed envelope", tls: actorTLS, md: metadata.Pairs(auth.DelegationMetadataKey, "broken"), code: codes.Unauthenticated},
		{name: "legacy ServiceAccount effective caller", tls: actorTLS, md: metadata.Pairs(auth.DelegationMetadataKey, string(legacyServiceAccountEnvelope())), code: codes.Unauthenticated},
		{name: "replaced effective Identity", tls: actorTLS, md: metadata.Pairs(auth.DelegationMetadataKey, string(wrongEncoded)), code: codes.PermissionDenied},
	} {
		t.Run(tc.name, func(t *testing.T) {
			conn, err := grpc.NewClient(address, grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(tc.tls)))
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
			conn, err := grpc.NewClient(address, grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)))
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

func TestServicesRequiresAnExactServiceCertificate(t *testing.T) {
	rule, err := auth.Services([]string{peertls.AppURI})
	if err != nil {
		t.Fatal(err)
	}
	empty, err := auth.Services(nil)
	if err != nil {
		t.Fatal(err)
	}
	service := servicePeerContext(t, peertls.AppURI)
	for _, tc := range []struct {
		name string
		rule auth.Rule
		ctx  context.Context
		code codes.Code
	}{
		{"exact service", rule, service, codes.OK},
		{"empty allowlist", empty, service, codes.PermissionDenied},
		{"zero rule", auth.Rule{}, service, codes.PermissionDenied},
		{"other service", rule, servicePeerContext(t, peertls.NotificationsURI), codes.PermissionDenied},
		{"workload SVID", rule, servicePeerContext(t, "spiffe://agentplane/fake-ns/fake-workload/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), codes.Unauthenticated},
		{"missing certificate", rule, t.Context(), codes.Unauthenticated},
		{"bearer only", rule, metadata.NewIncomingContext(t.Context(), metadata.Pairs("authorization", "Bearer test-token")), codes.Unauthenticated},
		{"bearer plus certificate", rule, metadata.NewIncomingContext(service, metadata.Pairs("authorization", "Bearer test-token")), codes.Unauthenticated},
		{"ungranted delegation", rule, metadata.NewIncomingContext(service, metadata.Pairs(auth.DelegationMetadataKey, "test-envelope")), codes.PermissionDenied},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, err := tc.rule.AdmitContext(tc.ctx, time.Second)
			if status.Code(err) != tc.code {
				t.Fatalf("admission: %v, want %s", err, tc.code)
			}
			if err != nil {
				return
			}
			admission, err := auth.Admitted(ctx)
			if err != nil || admission != (auth.ServiceAdmission{Caller: auth.ServicePrincipal{SPIFFEID: peertls.AppURI}}) {
				t.Fatalf("admission = %#v, %v", admission, err)
			}
			if _, err := auth.Caller(ctx); err == nil {
				t.Fatal("service acquired Pod-bound account proof")
			}
			if _, err := auth.EffectiveCaller(ctx); err == nil {
				t.Fatal("service acquired effective account authority")
			}
		})
	}
	for _, callers := range [][]string{{peertls.AppURI, peertls.AppURI}, {"spiffe://agentplane/app/"}, {"spiffe://other/app"}} {
		if _, err := auth.Services(callers); err == nil {
			t.Fatalf("accepted service allowlist %v", callers)
		}
	}

	methods := auth.Methods{healthpb.Health_Watch_FullMethodName: rule}
	admitted, err := methods.AdmitContext(service, healthpb.Health_Watch_FullMethodName, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err := auth.RevalidateStream(admitted); err != nil {
		t.Fatal(err)
	}
	// Expire the already verified connection's leaf without waiting on wall-clock timing.
	remote, _ := peer.FromContext(service)
	remote.AuthInfo.(credentials.TLSInfo).State.PeerCertificates[0].NotAfter = time.Now().Add(-time.Minute)
	if err := auth.RevalidateStream(admitted); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("expired stream admission: %v", err)
	}
}

func servicePeerContext(t *testing.T, uri string) context.Context {
	t.Helper()
	files := transporttesting.NewCA(t).Issue(t, uri)
	pair, err := tls.LoadX509KeyPair(files.CertFile, files.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	return peer.NewContext(t.Context(), &peer.Peer{AuthInfo: credentials.TLSInfo{State: tls.ConnectionState{PeerCertificates: []*x509.Certificate{leaf}}}})
}

func TestServicesDelegationKeepsItsOwnGrantsAndLiveIdentity(t *testing.T) {
	identity := auth.Identity{Name: "owner", UID: "owner-uid"}
	liveUID := identity.UID
	identities := identityUIDReaderFunc(func(_ context.Context, name string) (string, bool, error) {
		return liveUID, name == identity.Name, nil
	})
	rule, err := auth.Services(nil)
	if err != nil {
		t.Fatal(err)
	}
	rule, err = rule.AllowServiceDelegationFrom([]auth.ServiceDelegator{{SPIFFEID: peertls.MCPEdgeURI, ConnectionProvenance: true}}, identities)
	if err != nil {
		t.Fatal(err)
	}
	envelope := &delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: identity.Name, Uid: identity.UID}},
		Provenance: &delegationpb.Delegation_Connection{Connection: &delegationpb.ConnectionProvenance{
			ConnectionId: "018f4e32-6632-7e7f-83dd-7f90ccf84f68", GrantId: "018f4e32-6632-7e7f-83dd-7f90ccf84f69",
		}},
	}
	encoded, err := auth.MarshalDelegation(envelope)
	if err != nil {
		t.Fatal(err)
	}
	ctx := metadata.NewIncomingContext(servicePeerContext(t, peertls.MCPEdgeURI), metadata.Pairs(auth.DelegationMetadataKey, string(encoded)))
	methods := auth.Methods{healthpb.Health_Watch_FullMethodName: rule}
	admitted, err := methods.AdmitContext(ctx, healthpb.Health_Watch_FullMethodName, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	proof, err := auth.Admitted(admitted)
	delegated, ok := proof.(auth.DelegatedServiceIdentityAdmission)
	if err != nil || !ok || delegated.Actor.SPIFFEID != peertls.MCPEdgeURI || delegated.EffectiveCaller != identity {
		t.Fatalf("delegated proof: %#v, %v", proof, err)
	}
	if _, err := methods.AdmitContext(ctx, healthpb.Health_Check_FullMethodName, time.Second); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("unlisted method: %v", err)
	}
	withoutConnection, err := rule.AllowServiceDelegationFrom([]auth.ServiceDelegator{{SPIFFEID: peertls.MCPEdgeURI}}, identities)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := withoutConnection.AdmitContext(ctx, time.Second); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("ungranted Connection provenance: %v", err)
	}
	if err := auth.RevalidateStream(admitted); err != nil {
		t.Fatal(err)
	}
	liveUID = "replacement-uid"
	if err := auth.RevalidateStream(admitted); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("replaced delegated Identity: %v", err)
	}
}

func TestServiceDelegationRequiresExplicitServedMethodsAndIdentityReader(t *testing.T) {
	rule, err := auth.Services(nil)
	if err != nil {
		t.Fatal(err)
	}
	method := healthpb.Health_Check_FullMethodName
	methods := auth.Methods{method: rule}
	actors := []auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}}
	reader := identityUIDReaderFunc(func(context.Context, string) (string, bool, error) { return "test-uid", true, nil })
	for _, tc := range []struct {
		name   string
		names  []string
		actors []auth.ServiceDelegator
		reader auth.IdentityUIDReader
	}{
		{"duplicate method", []string{method, method}, actors, reader},
		{"unserved method", []string{healthpb.Health_Watch_FullMethodName}, actors, reader},
		{"missing actors", []string{method}, nil, reader},
		{"missing Identity reader", []string{method}, actors, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := methods.WithServiceDelegation(tc.names, tc.actors, tc.reader); err == nil {
				t.Fatal("invalid service delegation grant accepted")
			}
		})
	}
}

func TestServiceDelegationTracksTheLiveIdentityWithoutAnEnvironment(t *testing.T) {
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, policyapi.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	objects, err := client.New(api.Config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	namespace := "test-service-delegation-policies"
	if err := objects.Create(t.Context(), &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}); err != nil {
		t.Fatal(err)
	}
	identity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: "test-delegated-identity"}, Spec: policyapi.IdentitySpec{Description: "test delegated identity"}}
	if err := objects.Create(t.Context(), identity); err != nil {
		t.Fatal(err)
	}
	identities, err := auth.KubernetesIdentityUIDs(objects, namespace)
	if err != nil {
		t.Fatal(err)
	}
	rule, err := auth.Services(nil)
	if err != nil {
		t.Fatal(err)
	}
	actors := []auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}}
	rule, err = rule.AllowServiceDelegationFrom(actors, identities)
	if err != nil {
		t.Fatal(err)
	}
	envelope := &delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: identity.Name, Uid: string(identity.UID)}},
		Provenance:      &delegationpb.Delegation_Session{Session: &delegationpb.SessionProvenance{SessionId: "a57a540c-ed82-4b85-b914-73746dba4bad"}},
	}
	contextFor := func(actor string, envelope *delegationpb.Delegation) context.Context {
		encoded, err := auth.MarshalDelegation(envelope)
		if err != nil {
			t.Fatal(err)
		}
		return metadata.NewIncomingContext(servicePeerContext(t, actor), metadata.Pairs(auth.DelegationMetadataKey, string(encoded)))
	}
	method := healthpb.Health_Watch_FullMethodName
	methods := auth.Methods{method: rule}
	ctx := contextFor(peertls.ActionsURI, envelope)
	admitted, err := methods.AdmitContext(ctx, method, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	proof, err := auth.Admitted(admitted)
	delegated, ok := proof.(auth.DelegatedServiceIdentityAdmission)
	if err != nil || !ok || delegated.Actor.SPIFFEID != peertls.ActionsURI || delegated.EffectiveCaller != (auth.Identity{Name: identity.Name, UID: string(identity.UID)}) {
		t.Fatalf("service delegation admission = %#v, %v", proof, err)
	}
	if _, err := auth.EffectiveCaller(admitted); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("delegated Identity became a ServiceAccount: %v", err)
	}
	if _, err := methods.AdmitContext(contextFor(peertls.NotificationsURI, envelope), method, time.Second); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("untrusted actor = %v", err)
	}
	missing := &delegationpb.Delegation{EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: "test-missing-identity", Uid: "test-absent-uid"}}, Provenance: envelope.Provenance}
	if _, err := methods.AdmitContext(contextFor(peertls.ActionsURI, missing), method, time.Second); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("missing Identity = %v", err)
	}
	if err := objects.Delete(t.Context(), identity); err != nil {
		t.Fatal(err)
	}
	if err := auth.RevalidateStream(admitted); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("deleted Identity revalidation = %v", err)
	}
	successor := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: identity.Name}, Spec: identity.Spec}
	if err := objects.Create(t.Context(), successor); err != nil {
		t.Fatal(err)
	}
	if err := auth.RevalidateStream(admitted); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("same-named successor admitted with old UID = %v", err)
	}
	envelope.GetIdentity().Uid = string(successor.UID)
	if _, err := methods.AdmitContext(contextFor(peertls.ActionsURI, envelope), method, time.Second); err != nil {
		t.Fatalf("successor Identity = %v", err)
	}
	unavailable, err := rule.AllowServiceDelegationFrom(actors, identityUIDReaderFunc(func(context.Context, string) (string, bool, error) {
		return "", false, errors.New("test Identity reader unavailable")
	}))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := unavailable.AdmitContext(ctx, time.Second); status.Code(err) != codes.Unavailable {
		t.Fatalf("unreadable Identity = %v", err)
	}
}
