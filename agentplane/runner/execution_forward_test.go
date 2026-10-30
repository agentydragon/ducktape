package runner

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	executionpb "github.com/agentydragon/ducktape/agentplane/proto/executionpb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/runner/configuration"
	"github.com/agentydragon/ducktape/agentplane/runner/control"
	runnerexecpb "github.com/agentydragon/ducktape/agentplane/runner/runner_execpb"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/coder/websocket"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

type directExecutionTestClient struct {
	ctx         context.Context
	cancel      context.CancelFunc
	conn        *grpc.ClientConn
	runner      runnerservicepb.RunnerClient
	executor    runnerexecpb.RunnerExecClient
	callCtx     context.Context
	bootID      string
	incarnation string
	identity    configuration.SandboxIdentity
}

func startDirectExecutionTestClient(t *testing.T, r *Runner, incarnation string) *directExecutionTestClient {
	t.Helper()
	listener, server, err := r.control.Listen(r.execution)
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	ctx, cancel := context.WithTimeout(t.Context(), codexExecServerTestTimeout)
	credentials, err := r.config.Control.Security.ClientCredentials("")
	if err != nil {
		cancel()
		server.Stop()
		_ = listener.Close()
		t.Fatal(err)
	}
	conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(credentials))
	if err != nil {
		cancel()
		server.Stop()
		_ = listener.Close()
		t.Fatal(err)
	}
	runnerClient := runnerservicepb.NewRunnerClient(conn)
	watch, err := runnerClient.WatchState(ctx, &runnerservicepb.WatchStateRequest{
		SandboxUid: r.config.Launch.Sandbox.UID, IncarnationUid: incarnation, ConnectionEpoch: 1,
	})
	if err != nil {
		cancel()
		_ = conn.Close()
		server.Stop()
		_ = listener.Close()
		t.Fatal(err)
	}
	first, err := watch.Recv()
	if err != nil {
		cancel()
		_ = conn.Close()
		server.Stop()
		_ = listener.Close()
		t.Fatal(err)
	}
	callCtx := metadata.AppendToOutgoingContext(
		ctx,
		control.SandboxMetadata, r.config.Launch.Sandbox.UID,
		control.IncarnationMetadata, incarnation,
		control.BootMetadata, first.GetRunnerBootId(),
		control.EpochMetadata, "1",
	)
	t.Cleanup(func() {
		cancel()
		server.Stop()
		_ = listener.Close()
		_ = conn.Close()
	})
	return &directExecutionTestClient{
		ctx: ctx, cancel: cancel, conn: conn, runner: runnerClient,
		executor: runnerexecpb.NewRunnerExecClient(conn), callCtx: callCtx,
		bootID: first.GetRunnerBootId(), incarnation: incarnation, identity: r.config.Launch.Sandbox,
	}
}

func (h *directExecutionTestClient) open(ctx context.Context, sessionID, requestID string) (runnerexecpb.RunnerExec_OpenSessionClient, error) {
	return h.executor.OpenSession(ctx, &executionpb.OpenSessionRequest{Scope: h.scope(sessionID), RequestId: requestID})
}

func (h *directExecutionTestClient) scope(sessionID string) *executionpb.ExecutionScope {
	return &executionpb.ExecutionScope{
		Destination: &sandboxespb.SandboxDestination{
			Sandbox: h.identity.Name, SandboxUid: h.identity.UID,
			Owner: &sandboxespb.ServiceAccount{Namespace: h.identity.Namespace, Name: h.identity.Name},
		},
		ExpectedIncarnationUid: h.incarnation,
		SessionId:              sessionID,
	}
}

func (h *directExecutionTestClient) replaceWatch(incarnation string, epoch uint64) error {
	watch, err := h.runner.WatchState(h.ctx, &runnerservicepb.WatchStateRequest{
		SandboxUid: h.identity.UID, IncarnationUid: incarnation, ConnectionEpoch: epoch,
	})
	if err != nil {
		return err
	}
	first, err := watch.Recv()
	if err != nil {
		return err
	}
	h.incarnation = incarnation
	h.bootID = first.GetRunnerBootId()
	h.callCtx = metadata.AppendToOutgoingContext(
		h.ctx,
		control.SandboxMetadata, h.identity.UID,
		control.IncarnationMetadata, incarnation,
		control.BootMetadata, h.bootID,
		control.EpochMetadata, uint64String(epoch),
	)
	return nil
}

func startNativeExecutionTestServer(t *testing.T, notify bool) (string, <-chan string, <-chan string) {
	t.Helper()
	opened := make(chan string, 8)
	closed := make(chan string, 8)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		connection, err := websocket.Accept(w, request, nil)
		if err != nil {
			return
		}
		var sessionID string
		defer func() {
			connection.CloseNow()
			if sessionID != "" {
				select {
				case closed <- sessionID:
				default:
				}
			}
		}()
		_, payload, err := connection.Read(request.Context())
		if err != nil {
			return
		}
		var initialize struct {
			ID string `json:"id"`
		}
		if json.Unmarshal(payload, &initialize) != nil || initialize.ID == "" {
			return
		}
		sessionID = "native-" + initialize.ID
		response := []byte(fmt.Sprintf(`{"id":%q,"result":{"sessionId":%q,"environmentInfo":{"shell":{"name":"bash","path":"/bin/bash"}}}}`, initialize.ID, sessionID))
		if connection.Write(request.Context(), websocket.MessageText, response) != nil {
			return
		}
		if _, _, err := connection.Read(request.Context()); err != nil { // initialized notification
			return
		}
		opened <- sessionID
		if notify {
			payload := []byte(fmt.Sprintf(`{"method":"codex/event","params":{"sessionId":%q}}`, sessionID))
			if connection.Write(request.Context(), websocket.MessageText, payload) != nil {
				return
			}
		}
		for {
			if _, _, err := connection.Read(request.Context()); err != nil {
				return
			}
		}
	}))
	t.Cleanup(server.Close)
	return "ws" + strings.TrimPrefix(server.URL, "http"), opened, closed
}

func TestOpenSessionEndsWhenObserverEpochIsSuperseded(t *testing.T) {
	url, opened, closed := startNativeExecutionTestServer(t, false)
	r := newExecutorLifecycleRunnerWithURL(t, url, func(context.Context) (*supervision.Instance, error) {
		return newTestExecutorInstance(&atomic.Int32{}), nil
	}, nil)
	client := startDirectExecutionTestClient(t, r, "incarnation-1")
	stream, err := client.open(client.callCtx, "sampler-session", "open-before-supersession")
	if err != nil {
		t.Fatalf("OpenSession RPC: %v", err)
	}
	response, err := stream.Recv()
	if err != nil || response.GetStatus().GetOutcome() != executionpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("OpenSession before supersession = %v, err=%v", response, err)
	}
	select {
	case <-opened:
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("local execution session did not initialize")
	}

	if err := client.replaceWatch("incarnation-2", 2); err != nil {
		t.Fatalf("replace observer epoch: %v", err)
	}
	select {
	case closedID := <-closed:
		if closedID != response.GetExecutionSessionId() {
			t.Fatalf("observer supersession closed native session %q, want %q", closedID, response.GetExecutionSessionId())
		}
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("observer epoch supersession left its local WebSocket open")
	}
	if _, err := stream.Recv(); err == nil {
		t.Fatal("OpenSession remained open after observer supersession")
	}
	if r.execution.Active() {
		t.Fatal("observer supersession left an execution slot active")
	}
}
