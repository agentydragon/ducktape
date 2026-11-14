package runnercontrol

import (
	"context"
	"crypto/x509"
	"errors"
	"net"
	"strconv"
	"testing"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/workloadid"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type passiveRuntime struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	requests chan *environmentruntimepb.WatchStateRequest
	response *environmentruntimepb.WatchStateResponse
}

func (r *passiveRuntime) WatchState(
	request *environmentruntimepb.WatchStateRequest,
	stream grpc.ServerStreamingServer[environmentruntimepb.WatchStateResponse],
) error {
	r.requests <- request
	if err := stream.Send(r.response); err != nil {
		return err
	}
	<-stream.Context().Done()
	return stream.Context().Err()
}

func TestRuntimeWatcherUsesPassiveWorkloadMTLSAndForwardsFacts(t *testing.T) {
	const credentialID = "0123456789abcdef0123456789abcdef"
	uri, err := (workloadid.ID{Namespace: "agentplane", Name: "sandbox", CredentialID: credentialID}).URI()
	if err != nil {
		t.Fatal(err)
	}
	ca := peertlstesting.NewCA(t)
	runtimeTLS := ca.IssueWithUsage(t, uri, x509.ExtKeyUsageServerAuth)
	controllerTLS := ca.IssueWithUsage(t, peertls.SandboxControllerURI, x509.ExtKeyUsageClientAuth)
	serverCredentials, err := runtimeTLS.ServerCredentials(peertls.SandboxControllerURI)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(sandboxapi.EnvironmentRuntimeServicePort)))
	if err != nil {
		t.Fatalf("listen on Runtime test port: %v", err)
	}
	server := grpc.NewServer(grpc.Creds(serverCredentials))
	runtime := &passiveRuntime{
		requests: make(chan *environmentruntimepb.WatchStateRequest, 1),
		response: &environmentruntimepb.WatchStateResponse{
			SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid", EnvironmentRuntimeBootId: "runtime-boot",
			Facts: &environmentruntimestatepb.EnvironmentRuntimeFacts{},
		},
	}
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, runtime)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})

	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{
		Namespace: "agentplane", Name: "sandbox", UID: "sandbox-uid",
	}}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Namespace: sandbox.Namespace, Name: "sandbox-pod", UID: "pod-uid",
	}, Status: corev1.PodStatus{PodIP: "127.0.0.1"}}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	responses := make(chan *environmentruntimepb.WatchStateResponse, 1)
	result := make(chan error, 1)
	watcher := RuntimeWatcher{Security: peertls.Security{Mode: peertls.MutualTLS, TLS: &controllerTLS}}
	go func() {
		result <- watcher.Watch(ctx, sandbox, pod, credentialID, func(response *environmentruntimepb.WatchStateResponse) error {
			responses <- response
			return nil
		})
	}()

	select {
	case request := <-runtime.requests:
		if request.GetSandboxUid() != string(sandbox.UID) || request.GetIncarnationUid() != string(pod.UID) ||
			request.GetConnectionEpoch() != 0 || !request.GetObserveOnly() {
			t.Fatalf("WatchState request = %v, want passive watch for this Sandbox incarnation", request)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Runtime did not receive a direct mTLS WatchState request")
	}

	select {
	case response := <-responses:
		if response.GetEnvironmentRuntimeBootId() != "runtime-boot" || response.GetFacts() == nil {
			t.Fatalf("forwarded Runtime state = %v", response)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("RuntimeWatcher did not forward the state snapshot")
	}
	cancel()
	select {
	case err := <-result:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("Watch returned %v after cancellation, want context.Canceled", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("RuntimeWatcher did not stop after context cancellation")
	}
}
