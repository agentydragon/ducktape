package runtimeproxy

import (
	"context"
	"fmt"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/workloadid"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	tlstest "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/testscheme"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	testAudience = "test-gateway"
	testSandbox  = "test-sandbox-a"
	testPod      = "test-runner-pod-a"
)

var (
	// api is the API server the binary's tests share; each test's Sandbox is in a namespace of its own.
	api        *kubeapiserver.Server
	namespaces atomic.Int32

	dispatcherAccount = auth.Account{Namespace: "test-dispatchers", Name: "test-dispatcher"}
	appAccount        = auth.Account{Namespace: "test-apps", Name: "test-app"}
	executorAccount   = auth.Account{Namespace: "test-samplers", Name: "test-sampler"}
	controllerAccount = auth.Account{Namespace: "test-system", Name: "test-sandbox-controller"}
)

func serverMethods(t *testing.T, g *Server) auth.Methods {
	t.Helper()
	methods, err := g.Methods()
	if err != nil {
		t.Fatal(err)
	}
	return methods
}

func useAPIServer(server *kubeapiserver.Server) error {
	api = server
	return nil
}

// sandboxFixture is a Running Sandbox whose runtime Pod is Ready, in a namespace of the test's own, with
// valid legacy tokens used to verify that bearer callers are rejected.
type sandboxFixture struct {
	namespace                                              string
	sandbox                                                *sandboxapi.Sandbox
	pod                                                    *corev1.Pod
	objects                                                client.Client
	runtimeToken, appToken, executorToken, controllerToken string
	dispatcherToken, unboundDispatcherToken                string
	ca                                                     *tlstest.CA
	serverTLS, runtimeTLS                                  peertls.Config
}

func newSandboxFixture(t *testing.T) *sandboxFixture {
	t.Helper()
	ctx := t.Context()
	f := &sandboxFixture{namespace: fmt.Sprintf("test-sandboxes-%d", namespaces.Add(1))}
	var err error
	if f.objects, err = client.New(api.Config, client.Options{Scheme: testscheme.New(t)}); err != nil {
		t.Fatal(err)
	}
	if _, err := api.ServiceAccount(ctx, auth.Account{Namespace: f.namespace, Name: "default"}); err != nil {
		t.Fatal(err)
	}
	f.sandbox = &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Name: testSandbox, Namespace: f.namespace},
		Spec:       sandboxapi.SandboxSpec{Template: "test-template"},
	}
	if err := f.objects.Create(ctx, f.sandbox); err != nil {
		t.Fatal(err)
	}
	controller := true
	automount := false
	pods := api.Clientset.CoreV1().Pods(f.namespace)
	f.pod, err = pods.Create(ctx, &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: testPod, Namespace: f.namespace,
			Labels: map[string]string{sandboxapi.EnvironmentRuntimeCredentialMetadataKey: "test-environment-runtime-credential"},
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind, Name: testSandbox,
				UID: f.sandbox.UID, Controller: &controller,
			}},
		},
		Spec: corev1.PodSpec{
			ServiceAccountName:           "default",
			AutomountServiceAccountToken: &automount,
			Containers:                   []corev1.Container{{Name: "runner", Image: "registry.test/unused"}},
		},
	}, metav1.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	f.pod.Status = corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "192.0.2.20", Conditions: []corev1.PodCondition{{
		Type: corev1.PodReady, Status: corev1.ConditionTrue,
	}}}
	if f.pod, err = pods.UpdateStatus(ctx, f.pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	f.sandbox.Status = sandboxapi.SandboxStatus{
		OperatingMode: sandboxapi.OperatingModeRunning,
		Incarnation:   &sandboxapi.Incarnation{Name: f.pod.Name, UID: string(f.pod.UID)},
		Conditions: []metav1.Condition{{
			Type: sandboxapi.ConditionRunning, Status: metav1.ConditionTrue, Reason: "Ready", LastTransitionTime: metav1.Now(),
		}},
	}
	if err := f.objects.Status().Update(ctx, f.sandbox); err != nil {
		t.Fatal(err)
	}
	if f.runtimeToken, err = api.PodToken(ctx, f.pod, testAudience); err != nil {
		t.Fatal(err)
	}
	if f.dispatcherToken, err = api.WorkloadToken(ctx, dispatcherAccount, testAudience); err != nil {
		t.Fatal(err)
	}
	if f.unboundDispatcherToken, err = api.UnboundToken(ctx, dispatcherAccount, testAudience); err != nil {
		t.Fatal(err)
	}
	if f.appToken, err = api.WorkloadToken(ctx, appAccount, testAudience); err != nil {
		t.Fatal(err)
	}
	if f.executorToken, err = api.WorkloadToken(ctx, executorAccount, testAudience); err != nil {
		t.Fatal(err)
	}
	if f.controllerToken, err = api.WorkloadToken(ctx, controllerAccount, testAudience); err != nil {
		t.Fatal(err)
	}
	f.ca = tlstest.NewCA(t)
	f.serverTLS = f.ca.Issue(t, peertls.RuntimeProxyURI)
	uri, err := workloadid.ID{Namespace: f.namespace, Name: f.sandbox.Name, CredentialID: f.pod.Labels[sandboxapi.EnvironmentRuntimeCredentialMetadataKey]}.URI()
	if err != nil {
		t.Fatal(err)
	}
	f.runtimeTLS = f.ca.Issue(t, uri)
	return f
}

// newServer is a Server replica serving the fixture's namespace. It reads the API server directly.
func (f *sandboxFixture) newServer(t *testing.T, executorCallers ...string) *Server {
	t.Helper()
	health := watchhealth.New()
	health.MarkSynced()
	return f.newServerReading(t, f.objects, health, executorCallers...)
}

func (f *sandboxFixture) newServerReading(t *testing.T, objects client.Reader, health *watchhealth.Health, executorCallers ...string) *Server {
	t.Helper()
	server, err := New(Config{
		Namespace:      f.namespace,
		RunnerSecurity: peertls.Security{Mode: peertls.MutualTLS, TLS: &f.serverTLS},
		RuntimeReaders: []string{peertls.AppURI}, SessionServices: []string{peertls.SessionsURI},
		ExecutorServices: executorCallers, ControllerServices: []string{peertls.SandboxControllerURI},
		ControlTiming: ControlTiming{LivenessTimeout: 10 * time.Second},
	}, objects, health, f.identities(t, objects))
	if err != nil {
		t.Fatal(err)
	}
	return server
}

// identities reads the fixture's Identities through objects, as the Kubernetes-backed production wiring does.
func (f *sandboxFixture) identities(t *testing.T, objects client.Reader) auth.IdentityUIDReader {
	t.Helper()
	identities, err := auth.KubernetesIdentityUIDs(objects, f.namespace)
	if err != nil {
		t.Fatal(err)
	}
	return identities
}

func (f *sandboxFixture) callerContext(service string) context.Context {
	return metadata.NewOutgoingContext(context.WithValue(context.Background(), serviceConnectionKey{}, service), metadata.Pairs(
		sandboxapi.RuntimeProxySandboxMetadataKey, testSandbox,
		sandboxapi.RuntimeProxySandboxNamespaceMetadataKey, f.namespace,
		sandboxapi.RuntimeProxySandboxUIDMetadataKey, string(f.sandbox.UID),
		sandboxapi.RuntimeProxySandboxOwnerNamespaceMetadataKey, f.namespace,
		sandboxapi.RuntimeProxySandboxOwnerNameMetadataKey, testSandbox,
	))
}

// serviceConnectionKey selects a real TLS connection only inside this test process.
// It is never serialized or derived from wire credentials.
type serviceConnectionKey struct{}

type serviceConnections struct {
	connections map[string]*grpc.ClientConn
	target      string
	ca          *tlstest.CA
}

func (c *serviceConnections) connection(ctx context.Context) (*grpc.ClientConn, error) {
	selector := ctx.Value(serviceConnectionKey{})
	service, valid := selector.(string)
	if selector != nil && !valid {
		return nil, status.Error(codes.Unauthenticated, "invalid fixture service selector")
	}
	conn, ok := c.connections[service]
	if !ok {
		return nil, status.Error(codes.Unauthenticated, "unknown fixture service")
	}
	return conn, nil
}

func (c *serviceConnections) Invoke(ctx context.Context, method string, args, reply any, opts ...grpc.CallOption) error {
	conn, err := c.connection(ctx)
	if err != nil {
		return err
	}
	return conn.Invoke(ctx, method, args, reply, opts...)
}

func (c *serviceConnections) NewStream(ctx context.Context, desc *grpc.StreamDesc, method string, opts ...grpc.CallOption) (grpc.ClientStream, error) {
	conn, err := c.connection(ctx)
	if err != nil {
		return nil, err
	}
	return conn.NewStream(ctx, desc, method, opts...)
}

func serveServer(t *testing.T, g *Server, register func(*grpc.Server)) *serviceConnections {
	t.Helper()
	ca := tlstest.NewCA(t)
	tlsConfig, err := ca.Issue(t, peertls.SessionDispatcherURI).WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(append(serverMethods(t, g).ServerOptions(0), grpc.Creds(credentials.NewTLS(tlsConfig)))...)
	register(server)
	address := grpcserver.Serve(t, server)
	result := &serviceConnections{connections: make(map[string]*grpc.ClientConn), target: address, ca: ca}
	for _, service := range []string{"", peertls.AppURI, peertls.NotificationsURI, peertls.SessionsURI, peertls.SandboxControllerURI, peertls.CentralSamplerURI} {
		certificateService := service
		if certificateService == "" {
			certificateService = peertls.AppURI
		}
		config, err := ca.Issue(t, certificateService).ClientTLSConfig(peertls.SessionDispatcherURI)
		if err != nil {
			t.Fatal(err)
		}
		if service == "" {
			config.Certificates = nil
			config.GetClientCertificate = nil
		}
		conn, err := grpc.NewClient(result.target, grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(config)))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = conn.Close() })
		result.connections[service] = conn
	}
	return result
}

func runObserver(t *testing.T, g *Server, destination runtimeDestination) (context.CancelFunc, <-chan struct{}, <-chan error) {
	t.Helper()
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan struct{})
	result := make(chan error, 1)
	go func() {
		result <- g.observeRuntime(ctx, destination)
		close(done)
	}()
	t.Cleanup(func() {
		cancel()
		waitObserver(t, done)
	})
	return cancel, done, result
}

func waitObserver(t *testing.T, done <-chan struct{}) {
	t.Helper()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Runtime observer did not stop")
	}
}

func awaitConnection(t *testing.T, g *Server, sandboxUID, bootID string, done <-chan struct{}) *runtimeConnection {
	t.Helper()
	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	poll := time.NewTicker(time.Millisecond)
	defer poll.Stop()
	for {
		if connection, err := g.connections.holder(sandboxUID); err == nil && connection.bootID == bootID {
			return connection
		}
		select {
		case <-done:
			t.Fatalf("Runtime observer stopped before becoming callable at boot %q", bootID)
		case <-deadline.C:
			t.Fatalf("Runtime observer did not become callable at boot %q", bootID)
		case <-poll.C:
		}
	}
}
