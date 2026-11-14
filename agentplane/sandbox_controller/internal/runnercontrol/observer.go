package runnercontrol

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strconv"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/workloadid"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/observability"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc"
	corev1 "k8s.io/api/core/v1"
)

const runtimeWatchHeartbeatTimeout = 30 * time.Second

// RuntimeWatcher observes a runner's facts directly. It is passive: reconnect and
// incarnation-generation decisions belong to its caller.
type RuntimeWatcher struct {
	Security peertls.Security
}

type stateWatchResult struct {
	response *environmentruntimepb.WatchStateResponse
	err      error
}

// Watch follows one current Pod incarnation until the stream ends, becomes unhealthy,
// or its context is cancelled. The Sandbox Controller's observe-only request does not
// claim or supersede the Gateway's execution epoch.
func (w RuntimeWatcher) Watch(
	ctx context.Context,
	sandbox *sandboxapi.Sandbox,
	pod *corev1.Pod,
	credentialID string,
	receive func(*environmentruntimepb.WatchStateResponse) error,
) error {
	if sandbox == nil || pod == nil {
		return errors.New("Sandbox and runner Pod are required")
	}
	if receive == nil {
		return errors.New("state response receiver is required")
	}
	if sandbox.UID == "" || pod.UID == "" || pod.Status.PodIP == "" {
		return errors.New("Sandbox UID, runner Pod UID, and Pod IP are required")
	}
	peerURI, err := (workloadid.ID{
		Namespace: sandbox.Namespace, Name: sandbox.Name, CredentialID: credentialID,
	}).URI()
	if err != nil {
		return fmt.Errorf("derive runner peer identity: %w", err)
	}
	credentials, err := w.Security.ClientCredentials(peerURI)
	if err != nil {
		return fmt.Errorf("configure runner transport: %w", err)
	}
	target := net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(sandboxapi.EnvironmentRuntimeServicePort))
	conn, err := grpc.NewClient(
		target,
		grpc.WithTransportCredentials(credentials),
		grpc.WithNoProxy(),
		grpc.WithDisableRetry(),
		observability.GRPCClientOption(),
	)
	if err != nil {
		return fmt.Errorf("create Environment Runtime connection: %w", err)
	}
	defer conn.Close()

	watchCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	watch, err := environmentruntimepb.NewEnvironmentRuntimeClient(conn).WatchState(watchCtx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: string(sandbox.UID), IncarnationUid: string(pod.UID), ConnectionEpoch: 0, ObserveOnly: true,
	})
	if err != nil {
		return fmt.Errorf("start observe-only Runtime state watch: %w", err)
	}

	responses := make(chan stateWatchResult, 1)
	go func() {
		for {
			response, err := watch.Recv()
			select {
			case responses <- stateWatchResult{response: response, err: err}:
			case <-watchCtx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()

	liveness := time.NewTimer(runtimeWatchHeartbeatTimeout)
	defer liveness.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-liveness.C:
			return errors.New("Environment Runtime state heartbeat timed out")
		case received := <-responses:
			if received.err != nil {
				if ctx.Err() != nil {
					return ctx.Err()
				}
				return received.err
			}
			if err := w.Security.AuthorizePeer(watch.Context(), peerURI); err != nil {
				return fmt.Errorf("authenticate Runtime peer: %w", err)
			}
			response := received.response
			if response == nil || response.GetSandboxUid() != string(sandbox.UID) ||
				response.GetIncarnationUid() != string(pod.UID) || response.GetConnectionEpoch() != 0 || response.GetFacts() == nil {
				return errors.New("Environment Runtime state has invalid identity or facts")
			}
			if err := receive(response); err != nil {
				return err
			}
			if !liveness.Stop() {
				select {
				case <-liveness.C:
				default:
				}
			}
			liveness.Reset(runtimeWatchHeartbeatTimeout)
		}
	}
}
