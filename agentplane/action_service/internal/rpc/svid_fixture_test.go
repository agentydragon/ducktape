package rpc

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/eventually"
	"github.com/google/uuid"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kuberuntime "k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	certificatePrefix       = "certificate:"
	callerCertificate       = certificatePrefix + "caller"
	otherCertificate        = certificatePrefix + "other-caller"
	readerCertificate       = certificatePrefix + "notifications"
	secondReaderCertificate = certificatePrefix + "app"
	edgeCertificate         = certificatePrefix + "mcp-edge"
	samplerCertificate      = certificatePrefix + "central-sampler"
)

type certificateConnectionKey struct{}

// certificateConnections selects an actual mTLS connection before sending a call. An absent
// selector uses certificate-less TLS; an unknown selector fails rather than gaining a certificate.
// Neither the selector nor a replacement Authorization header crosses the receiver boundary.
type certificateConnections struct {
	transports  map[string]credentials.TransportCredentials
	connections map[string]*grpc.ClientConn
	records     map[string]*workloadapi.WorkloadCredential
	objects     client.Client
}

func (c *certificateConnections) connection(ctx context.Context) (*grpc.ClientConn, error) {
	selector, _ := ctx.Value(certificateConnectionKey{}).(string)
	connection := c.connections[selector]
	if connection == nil {
		return nil, status.Error(codes.Unauthenticated, "unknown fixture certificate connection")
	}
	return connection, nil
}

func (c *certificateConnections) Invoke(ctx context.Context, method string, args, reply any, opts ...grpc.CallOption) error {
	connection, err := c.connection(ctx)
	if err != nil {
		return err
	}
	return connection.Invoke(ctx, method, args, reply, opts...)
}

func (c *certificateConnections) NewStream(ctx context.Context, desc *grpc.StreamDesc, method string, opts ...grpc.CallOption) (grpc.ClientStream, error) {
	connection, err := c.connection(ctx)
	if err != nil {
		return nil, err
	}
	return connection.NewStream(ctx, desc, method, opts...)
}

func (s *served) configureCertificates(t *testing.T) (credentials.TransportCredentials, *auth.SVIDResolver) {
	t.Helper()
	ca := transporttesting.NewCA(t)
	server := ca.Issue(t, peertls.ActionsURI)
	config := listener.Config{ListenAddress: "127.0.0.1:0", TLS: &listener.TLS{CertFile: server.CertFile, KeyFile: server.KeyFile}, Auth: []listener.Authentication{&listener.SPIFFEAuth{CAFile: ca.RootFile}}}
	serverCredentials, err := config.ServerCredentialsWithServices(peertls.ActionsURI)
	if err != nil {
		t.Fatal(err)
	}
	scheme := kuberuntime.NewScheme()
	for _, add := range []func(*kuberuntime.Scheme) error{workloadapi.AddToScheme, policyapi.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	objects, err := client.New(kube.Config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	bare, err := (&peertls.ClientTLS{CAFile: ca.RootFile, ServerURI: peertls.ActionsURI}).Credentials()
	if err != nil {
		t.Fatal(err)
	}
	s.connections = &certificateConnections{transports: map[string]credentials.TransportCredentials{"": bare}, connections: map[string]*grpc.ClientConn{}, records: map[string]*workloadapi.WorkloadCredential{}, objects: objects}
	if err := objects.Create(t.Context(), &policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Namespace: s.policy, Name: "fixture-admission"}, Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{exactActions("fixture-unused", "none")}}}); err != nil {
		t.Fatal(err)
	}
	for selector, name := range map[string]string{callerCertificate: callerAccount.Name, otherCertificate: otherAccount.Name} {
		identity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: s.policy, Name: name}, Spec: policyapi.IdentitySpec{Description: "Action integration caller"}}
		if err := objects.Create(t.Context(), identity); err != nil {
			t.Fatal(err)
		}
		owner := store.IdentityPrincipal{Identity: store.Identity{Name: name, UID: string(identity.UID)}}
		if selector == callerCertificate {
			s.caller = owner
		} else {
			s.otherCaller = owner
		}
		record := &workloadapi.WorkloadCredential{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "action-" + uuid.NewString()}, Spec: workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{Identity: &workloadapi.IdentitySubject{Name: name, UID: identity.UID}}}}
		if err := objects.Create(t.Context(), record); err != nil {
			t.Fatal(err)
		}
		id := strings.ReplaceAll(uuid.NewString(), "-", "")
		uri := fmt.Sprintf("spiffe://agentplane/%s/%s/%s", record.Namespace, record.Name, id)
		record.Status = workloadapi.WorkloadCredentialStatus{ObservedGeneration: record.Generation, CredentialID: id, URI: uri, SecretName: "svid-" + record.Name, Conditions: []metav1.Condition{{Type: workloadapi.ConditionReady, Status: metav1.ConditionTrue, Reason: "Issued", Message: "test issued", LastTransitionTime: metav1.Now(), ObservedGeneration: record.Generation}}}
		if err := objects.Status().Update(t.Context(), record); err != nil {
			t.Fatal(err)
		}
		s.connections.records[selector] = record
		issued := ca.Issue(t, uri)
		s.connections.transports[selector], err = issued.ClientCredentials(peertls.ActionsURI)
		if err != nil {
			t.Fatal(err)
		}
		binding := &policyapi.PolicyBinding{ObjectMeta: metav1.ObjectMeta{Namespace: s.policy, Name: "admit-" + name}, Spec: policyapi.PolicyBindingSpec{Subject: policyapi.Subject{Identity: &policyapi.IdentityRef{Name: name}}, PolicySets: []string{"fixture-admission"}}}
		if err := objects.Create(t.Context(), binding); err != nil {
			t.Fatal(err)
		}
		eventually.Eventually(t, "the workload Identity binding", func() bool { return s.index.AdmitsIdentity(auth.Identity{Name: name, UID: string(identity.UID)}) })
	}
	for selector, uri := range map[string]string{readerCertificate: peertls.NotificationsURI, secondReaderCertificate: peertls.AppURI, edgeCertificate: peertls.MCPEdgeURI, samplerCertificate: peertls.CentralSamplerURI} {
		issued := ca.Issue(t, uri)
		s.connections.transports[selector], err = issued.ClientCredentials(peertls.ActionsURI)
		if err != nil {
			t.Fatal(err)
		}
	}
	svid, err := auth.NewSVIDResolver(objects, []string{testNamespace}, &resolver.Identities{PolicyNamespace: s.policy})
	if err != nil {
		t.Fatal(err)
	}
	return serverCredentials, svid
}

func (s *served) connectCertificates(t *testing.T, address string) {
	t.Helper()
	s.certificateURLs = map[string]string{}
	for selector, transport := range s.connections.transports {
		connection, err := grpc.NewClient(address, grpc.WithTransportCredentials(transport))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { connection.Close() })
		s.connections.connections[selector] = connection
		// Each facade uses a fixed real mTLS connection. HTTP credentials remain untouched.
		registration := func(register facade.Registration) facade.Registration {
			return func(ctx context.Context, mux *runtime.ServeMux, target string, options []grpc.DialOption) error {
				return register(ctx, mux, target, append(options, grpc.WithTransportCredentials(transport)))
			}
		}
		handler, err := facade.NewHandler(t.Context(), address, 0, registration(action_servicepb.RegisterActionServiceHandlerFromEndpoint), registration(operator_action_servicepb.RegisterOperatorActionServiceHandlerFromEndpoint))
		if err != nil {
			t.Fatal(err)
		}
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		server := &http.Server{Handler: handler, ReadHeaderTimeout: callDeadline}
		go func() { _ = server.Serve(listener) }()
		t.Cleanup(func() { server.Close() })
		s.certificateURLs[selector] = "http://" + listener.Addr().String()
	}
	s.url = s.certificateURLs[""]
}

func (s *served) facadeURL(selector string) string {
	if strings.HasPrefix(selector, certificatePrefix) {
		return s.certificateURLs[selector]
	}
	return s.url
}

func (s *served) revokeCertificate(t *testing.T, selector string) {
	t.Helper()
	if err := s.connections.objects.Delete(t.Context(), s.connections.records[selector]); err != nil {
		t.Fatal(err)
	}
}

func (s *served) removeCallerBinding(t *testing.T) {
	t.Helper()
	binding := &policyapi.PolicyBinding{ObjectMeta: metav1.ObjectMeta{Namespace: s.policy, Name: "admit-" + s.caller.Identity.Name}}
	if err := policyObjects.Delete(t.Context(), binding); err != nil {
		t.Fatal(err)
	}
	eventually.Eventually(t, "the caller binding removal", func() bool {
		return !s.index.AdmitsIdentity(auth.Identity{Name: s.caller.Identity.Name, UID: s.caller.Identity.UID})
	})
}
