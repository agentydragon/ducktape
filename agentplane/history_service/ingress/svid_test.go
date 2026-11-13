package ingress

import (
	"net"
	"slices"
	"sync/atomic"
	"testing"
	"time"

	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	session_ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/api/ingress/session_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestSVIDKeepsTheFirstWriterIdentityGate(t *testing.T) {
	namespace := "history-svid-" + uuid.NewString()
	first := workloadauthtesting.NewIdentityWorkload(t, kube, namespace, "first-writer", testPolicyNamespace).Workload
	other := workloadauthtesting.NewIdentityWorkload(t, kube, namespace, "other-writer", testPolicyNamespace).Workload
	ca := transporttesting.NewCA(t)
	svids, err := auth.NewKubernetesSVIDResolver(kube.Config, []string{namespace}, &resolver.Identities{PolicyNamespace: testPolicyNamespace})
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := auth.NewResolver(kube.TokenReviews(), testAudience, []string{namespace})
	if err != nil {
		t.Fatal(err)
	}
	service, _ := newService(t, Timing{IdleAck: time.Hour, SilenceLimit: 2 * time.Hour})
	serverCredentials := ca.Issue(t, transport.HistoryIngressURI)
	rpc := listener.Config{
		ListenAddress: "127.0.0.1:0",
		TLS:           &listener.TLS{CertFile: serverCredentials.CertFile, KeyFile: serverCredentials.KeyFile},
		Auth: []listener.Authentication{
			&listener.SPIFFEAuth{CAFile: serverCredentials.CAFile},
			&listener.KubernetesBearerAuth{Audiences: []string{testAudience}},
		},
	}
	resolver, err = resolver.WithListenerAuthentication(rpc, svids)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS, err := rpc.ServerCredentials(transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{session_ingresspb.SessionJournalIngress_Push_FullMethodName: auth.SVIDOrAccounts(resolver, nil)}
	var pushHandlers atomic.Int32
	server := grpc.NewServer(
		grpc.ChainStreamInterceptor(methods.StreamInterceptor(time.Second), func(server any, stream grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
			if info.FullMethod == session_ingresspb.SessionJournalIngress_Push_FullMethodName {
				pushHandlers.Add(1)
			}
			return handler(server, stream)
		}),
		grpc.Creds(serverTLS),
	)
	session_ingresspb.RegisterSessionJournalIngressServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	idleListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	idleService := &Service{History: service.History, Timing: Timing{IdleAck: testTiming.IdleAck, SilenceLimit: time.Hour}}
	idleServer := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(serverTLS))...)
	session_ingresspb.RegisterSessionJournalIngressServer(idleServer, idleService)
	go func() { _ = idleServer.Serve(idleListener) }()
	t.Cleanup(idleServer.Stop)
	connectAt := func(address string, workload workloadauthtesting.Workload) testIngress {
		config := ca.Issue(t, workload.URI)
		security, err := config.ClientCredentials(transport.HistoryIngressURI)
		if err != nil {
			t.Fatal(err)
		}
		connection, err := grpc.NewClient(address, grpc.WithTransportCredentials(security), grpc.WithNoProxy())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = connection.Close() })
		return testIngress{history: service.History, client: session_ingresspb.NewSessionJournalIngressClient(connection)}
	}
	connect := func(workload workloadauthtesting.Workload) testIngress {
		return connectAt(listener.Addr().String(), workload)
	}
	session := uuid.New()
	// Mixed certificate and bearer credentials are rejected before Push reaches its handler.
	firstIngress := connect(first)
	mixed := firstIngress.open(t, "forged")
	mixed.send(announce(uuid.New(), 0, ""))
	if code := mixed.refused(); code != codes.Unauthenticated {
		t.Fatalf("mixed SVID and bearer credentials: %v", code)
	}
	if got := pushHandlers.Load(); got != 0 {
		t.Fatalf("mixed credentials reached Push handler %d times", got)
	}
	// The established SVID call retains its admitted writer through Sandbox deletion and final seal.
	admitted := firstIngress.open(t, "")
	admitted.send(append([]*session_ingresspb.PushFrame{announce(session, 0, "")}, entries(1)...)...)
	admitted.acknowledged(1, false)
	if got := pushHandlers.Load(); got != 1 {
		t.Fatalf("SVID call reached Push handler %d times, want 1", got)
	}
	refused := connect(other).open(t, "")
	refused.send(announce(session, 0, ""))
	if code := refused.refused(); code != codes.PermissionDenied {
		t.Fatalf("different SVID writer: %v", code)
	}
	if got := committed(t, service.History, session); len(got) != 1 {
		t.Fatalf("committed %v", got)
	}
	entryRevocationSession := uuid.New()
	entryRevocation := firstIngress.open(t, "")
	entryRevocation.send(append([]*session_ingresspb.PushFrame{announce(entryRevocationSession, 0, "")}, entries(1)...)...)
	entryRevocation.acknowledged(1, false)
	heartbeatRevocationSession := uuid.New()
	heartbeatRevocation := firstIngress.open(t, "")
	heartbeatRevocation.send(append([]*session_ingresspb.PushFrame{announce(heartbeatRevocationSession, 0, "")}, entries(1)...)...)
	heartbeatRevocation.acknowledged(1, false)
	sealRevocationSession := uuid.New()
	sealRevocation := firstIngress.open(t, "")
	sealRevocation.send(append([]*session_ingresspb.PushFrame{announce(sealRevocationSession, 0, "")}, entries(1)...)...)
	sealRevocation.acknowledged(1, false)

	// Deletion retains the credential while Runtime flushes its final History seal.
	first.Sandbox.Finalizers = []string{"test.example/keep-until-sealed"}
	if err := first.Objects.Update(t.Context(), first.Sandbox); err != nil {
		t.Fatal(err)
	}
	if err := first.Objects.Delete(t.Context(), first.Sandbox); err != nil {
		t.Fatal(err)
	}
	if err := first.Objects.Get(t.Context(), client.ObjectKeyFromObject(first.Sandbox), first.Sandbox); err != nil {
		t.Fatal(err)
	}
	first.Sandbox.Status.StopStage = sandboxapi.StopStageQuiescing
	if err := first.Objects.Status().Update(t.Context(), first.Sandbox); err != nil {
		t.Fatal(err)
	}
	admitted.send(sealed(1, first.Principal.PodUID))
	admitted.acknowledged(1, true)

	// Clearing the binding revokes durable writes on an already-admitted stream.
	idleRevocation := connectAt(idleListener.Addr().String(), first).open(t, "")
	idleRevocation.send(announce(uuid.New(), 0, ""))
	idleRevocation.acknowledged(0, false)
	first.Record.Spec.Binding, first.Record.Spec.Pod = nil, nil
	if err := first.Objects.Update(t.Context(), first.Record); err != nil {
		t.Fatal(err)
	}
	if code := idleRevocation.refused(); code != codes.Unauthenticated {
		t.Fatalf("revoked idle acknowledgement: %v", code)
	}
	heartbeatRevocation.send(heartbeat())
	if code := heartbeatRevocation.refused(); code != codes.Unauthenticated {
		t.Fatalf("revoked heartbeat: %v", code)
	}
	entryRevocation.send(append(entries(2), heartbeat())...)
	if code := entryRevocation.refused(); code != codes.Unauthenticated {
		t.Fatalf("revoked established stream: %v", code)
	}
	if got := committed(t, service.History, entryRevocationSession); len(got) != 1 {
		t.Fatalf("revoked stream committed %v", got)
	}

	// Removing the Sandbox, and with it its record, also revokes an established stream before it
	// can write a seal.
	first.Sandbox.Finalizers = nil
	if err := first.Objects.Update(t.Context(), first.Sandbox); err != nil {
		t.Fatal(err)
	}
	if err := first.Objects.Get(t.Context(), client.ObjectKeyFromObject(first.Sandbox), first.Sandbox); !apierrors.IsNotFound(err) {
		t.Fatalf("Sandbox after finalizer removal: %v", err)
	}
	first.Revoke(t)
	sealRevocation.send(sealed(1, first.Principal.PodUID))
	if code := sealRevocation.refused(); code != codes.Unauthenticated {
		t.Fatalf("removed Sandbox established seal: %v", code)
	}
	page, err := service.History.ReadPage(t.Context(), sealRevocationSession, 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if page.FeedState.GetSealed() != nil {
		t.Fatalf("revoked stream recorded seal %v", page.FeedState.GetSealed())
	}
}

const testPolicyNamespace = "test-history-policies"

// serveSVIDIngress serves service on a SPIFFE listener admitting Push as History does, resolving
// Identity subjects in testPolicyNamespace, and returns a client for a workload certificate URI.
func serveSVIDIngress(t *testing.T, service *Service, namespace string) func(uri string) testIngress {
	t.Helper()
	ca := transporttesting.NewCA(t)
	svids, err := auth.NewKubernetesSVIDResolver(kube.Config, []string{namespace}, &resolver.Identities{PolicyNamespace: testPolicyNamespace})
	if err != nil {
		t.Fatal(err)
	}
	principals, err := auth.NewResolver(kube.TokenReviews(), testAudience, []string{namespace})
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials := ca.Issue(t, transport.HistoryIngressURI)
	rpc := listener.Config{
		ListenAddress: "127.0.0.1:0",
		TLS:           &listener.TLS{CertFile: serverCredentials.CertFile, KeyFile: serverCredentials.KeyFile},
		Auth:          []listener.Authentication{&listener.SPIFFEAuth{CAFile: serverCredentials.CAFile}},
	}
	principals, err = principals.WithListenerAuthentication(rpc, svids)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS, err := rpc.ServerCredentials(transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	socket, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{session_ingresspb.SessionJournalIngress_Push_FullMethodName: auth.SVIDOrAccounts(principals, nil)}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(serverTLS))...)
	session_ingresspb.RegisterSessionJournalIngressServer(server, service)
	go func() { _ = server.Serve(socket) }()
	t.Cleanup(server.Stop)
	return func(uri string) testIngress {
		security, err := ca.Issue(t, uri).ClientCredentials(transport.HistoryIngressURI)
		if err != nil {
			t.Fatal(err)
		}
		connection, err := grpc.NewClient(socket.Addr().String(), grpc.WithTransportCredentials(security), grpc.WithNoProxy())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = connection.Close() })
		return testIngress{history: service.History, client: session_ingresspb.NewSessionJournalIngressClient(connection)}
	}
}

func announceExecuting(sessionID uuid.UUID, identity *policyapi.Identity) *session_ingresspb.PushFrame {
	frame := announce(sessionID, 0, "")
	attached := frame.GetAnnounce()
	if identity != nil {
		attached.ExecutionIdentity = &identitypb.Identity{Name: identity.Name, Uid: string(identity.UID)}
	}
	return frame
}

// readsAs reports whether a read scoped to identity lists sessionID.
func readsAs(t *testing.T, history *storage.Store, identity *policyapi.Identity, sessionID uuid.UUID) bool {
	t.Helper()
	sessions, err := history.WithReadScope(storage.OwnedByIdentity(storage.Identity{Name: identity.Name, UID: string(identity.UID)})).
		RecordedSessions(t.Context(), 0, 1000)
	if err != nil {
		t.Fatal(err)
	}
	return slices.ContainsFunc(sessions, func(session storage.RecordedSession) bool { return session.SessionID == sessionID })
}

func TestAnIdentityRunnerWritesAsItsIdentityPinnedToItsSandbox(t *testing.T) {
	namespace := "history-identity-" + uuid.NewString()
	first := workloadauthtesting.NewIdentityWorkload(t, kube, namespace, "test-first-sandbox", testPolicyNamespace)
	sibling := workloadauthtesting.RunAs(t, workloadauthtesting.NewWorkload(t, kube, namespace, "test-sibling-sandbox"), first.Identity)
	other := workloadauthtesting.NewIdentityWorkload(t, kube, namespace, "test-other-sandbox", testPolicyNamespace)
	account := workloadauthtesting.NewWorkload(t, kube, namespace, "test-account-sandbox")
	service, _ := newService(t, testTiming)
	service.SandboxUIDs = NewServiceAccountSandboxUIDResolver(kube.Clientset)
	connect := serveSVIDIngress(t, service, namespace)

	session := uuid.New()
	pushed := connect(first.URI).open(t, "")
	pushed.send(append([]*session_ingresspb.PushFrame{announceExecuting(session, first.Identity)}, entries(1)...)...)
	pushed.acknowledged(1, false)
	if !readsAs(t, service.History, first.Identity, session) || readsAs(t, service.History, other.Identity, session) {
		t.Fatal("an Identity-run Session reads by exactly its execution Identity")
	}
	for name, workload := range map[string]workloadauthtesting.Workload{
		"the same Identity in another Sandbox": sibling.Workload,
		"another Identity":                     other.Workload,
		"an Identity without writer authority": account,
	} {
		refused := connect(workload.URI).open(t, "")
		refused.send(announce(session, 1, ""))
		if code := refused.refused(); code != codes.PermissionDenied && code != codes.Unauthenticated {
			t.Fatalf("%s pushed the Session: %v", name, code)
		}
	}
	for name, test := range map[string]struct {
		frame *session_ingresspb.PushFrame
		code  codes.Code
	}{
		"another execution Identity":       {announceExecuting(uuid.New(), other.Identity), codes.PermissionDenied},
		"an incomplete execution Identity": {announceExecuting(uuid.New(), &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Name: first.Identity.Name}}), codes.InvalidArgument},
	} {
		refused := connect(first.URI).open(t, "")
		refused.send(test.frame)
		if code := refused.refused(); code != test.code {
			t.Fatalf("an Identity runner announced %s: %v, want %v", name, code, test.code)
		}
	}

	// A same-named successor Identity in the same Sandbox is another writer.
	first.Delete(t)
	if err := first.Objects.Delete(t.Context(), first.Identity); err != nil {
		t.Fatal(err)
	}
	if !readsAs(t, service.History, first.Identity, session) {
		t.Fatal("a retained Session stopped reading once its Sandbox and Identity were deleted")
	}
	successor := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: testPolicyNamespace, Name: first.Identity.Name},
		Spec:       policyapi.IdentitySpec{Description: "test successor agent"},
	}
	if err := first.Objects.Create(t.Context(), successor); err != nil {
		t.Fatal(err)
	}
	recreated := workloadauthtesting.RunAs(t, workloadauthtesting.NewWorkload(t, kube, namespace, "test-recreated-sandbox"), successor)
	refused := connect(recreated.URI).open(t, "")
	refused.send(announce(session, 1, ""))
	if code := refused.refused(); code != codes.PermissionDenied {
		t.Fatalf("a successor Identity pushed its predecessor's Session: %v", code)
	}
}
