package gateway

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var errForwardOwned = status.Error(codes.Unavailable, connectionHeldByAnotherReplica)

type forwardClientConn struct {
	conn     *grpc.ClientConn
	identity metadata.MD
}

func (c *forwardClientConn) Invoke(ctx context.Context, method string, args, reply any, opts ...grpc.CallOption) error {
	return c.conn.Invoke(metadata.NewOutgoingContext(ctx, c.identity.Copy()), method, args, reply, opts...)
}

func (c *forwardClientConn) NewStream(ctx context.Context, desc *grpc.StreamDesc, method string, opts ...grpc.CallOption) (grpc.ClientStream, error) {
	return c.conn.NewStream(metadata.NewOutgoingContext(ctx, c.identity.Copy()), desc, method, opts...)
}

// RunForward supervises one observer per local forward runner. The existing Lease elects
// one Gateway replica; connection availability never chooses command queue ownership.
func (g *Gateway) RunForward(ctx context.Context) error {
	enabled := false
	for _, mode := range g.config.RunnerTransports {
		enabled = enabled || mode == "forward"
	}
	if !enabled {
		<-ctx.Done()
		return ctx.Err()
	}
	type activeRunner struct {
		destination runnerDestination
		cancel      context.CancelFunc
		done        chan struct{}
	}
	active := make(map[string]activeRunner)
	var workers sync.WaitGroup
	defer func() {
		for _, runner := range active {
			runner.cancel()
		}
		workers.Wait()
	}()
	ticker := time.NewTicker(g.config.ControlTiming.HeartbeatInterval)
	defer ticker.Stop()
	for {
		var resources sandboxapi.SandboxList
		if err := g.objects.List(ctx, &resources, client.InNamespace(g.config.Namespace)); err != nil {
			return fmt.Errorf("list forward Sandboxes: %w", err)
		}
		wanted := make(map[string]runnerDestination)
		for i := range resources.Items {
			destination, err := g.runnerDestination(ctx, &resources.Items[i])
			if err == nil && destination.mode == "forward" {
				wanted[destination.identity.UID] = destination
			}
		}
		for uid, runner := range active {
			current, exists := wanted[uid]
			select {
			case <-runner.done:
				delete(active, uid)
			default:
				if !exists || current != runner.destination {
					runner.cancel()
					delete(active, uid)
				}
			}
		}
		for uid, destination := range wanted {
			if _, exists := active[uid]; exists {
				continue
			}
			workerCtx, cancel := context.WithCancel(ctx)
			done := make(chan struct{})
			active[uid] = activeRunner{destination, cancel, done}
			workers.Add(1)
			go func() {
				defer workers.Done()
				defer close(done)
				defer cancel()
				if err := g.observeForward(workerCtx, destination); err != nil && workerCtx.Err() == nil && !errors.Is(err, errForwardOwned) {
					slog.Warn("forward runner observer ended", "sandbox", destination.identity.Name, "error", err)
				}
			}()
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

type observedState struct {
	response *runnerservicepb.WatchStateResponse
	err      error
}

func (g *Gateway) observeForward(ctx context.Context, destination runnerDestination) error {
	timing := g.config.ControlTiming
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	creds, err := g.config.RunnerTLS.ClientCredentials(destination.peerURI)
	if err != nil {
		return err
	}
	conn, err := grpc.NewClient(destination.target, grpc.WithTransportCredentials(creds), grpc.WithNoProxy(),
		grpc.WithDisableRetry(), observability.GRPCClientOption())
	if err != nil {
		return err
	}
	defer conn.Close()
	claimCtx, release := context.WithTimeout(ctx, timing.LivenessTimeout)
	epoch, err := g.records.claimForward(claimCtx, destination.identity, destination.podUID)
	release()
	if err != nil {
		return err
	}
	watch, err := runnerservicepb.NewRunnerClient(conn).WatchState(ctx, &runnerservicepb.WatchStateRequest{
		SandboxUid: destination.identity.UID, IncarnationUid: destination.podUID, ConnectionEpoch: epoch,
	})
	if err != nil {
		return err
	}
	responses := make(chan observedState, 1)
	go func() {
		for {
			response, err := watch.Recv()
			select {
			case responses <- observedState{response, err}:
			case <-ctx.Done():
				return
			}
			if err != nil {
				return
			}
		}
	}()
	liveness := time.NewTimer(timing.LivenessTimeout)
	defer liveness.Stop()
	maximumAge := time.NewTimer(timing.MaximumAge)
	defer maximumAge.Stop()
	var connection *runnerConnection
	defer func() {
		if connection != nil {
			g.connections.remove(connection, status.Error(codes.Unavailable, "forward runner observation ended"))
		}
	}()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-liveness.C:
			return status.Error(codes.Unavailable, "forward runner state timed out")
		case <-maximumAge.C:
			return status.Error(codes.Unavailable, "maximum connection age reached")
		case received := <-responses:
			if received.err != nil {
				return received.err
			}
			response := received.response
			if identity, err := runner_transport.PeerIdentity(watch.Context()); err != nil || identity != destination.peerURI {
				return status.Error(codes.Unauthenticated, "forward runner certificate is no longer valid")
			}
			if response.GetRunnerBootId() == "" || response.GetSandboxUid() != destination.identity.UID || response.GetIncarnationUid() != destination.podUID || response.GetConnectionEpoch() != epoch || response.GetFacts() == nil {
				return status.Error(codes.InvalidArgument, "forward runner state has invalid identity or facts")
			}
			if connection != nil && connection.bootID != response.RunnerBootId {
				return status.Error(codes.Aborted, "runner boot changed")
			}
			facts, err := sandboxlease.FromProto(response.GetFacts())
			if err != nil {
				return status.Error(codes.InvalidArgument, "forward runner sent invalid facts")
			}
			validateCtx, release := context.WithTimeout(ctx, timing.LivenessTimeout)
			_, currentUID, err := g.certificatePeer(validateCtx, destination.peerURI, "forward")
			if err == nil && currentUID != destination.podUID {
				err = status.Error(codes.PermissionDenied, "runner incarnation changed")
			}
			if err == nil && connection == nil {
				err = g.records.recordBootID(validateCtx, destination.identity, epoch, response.RunnerBootId)
			}
			if err == nil {
				err = g.records.renew(validateCtx, destination.identity, epoch, time.Now(), &facts)
			}
			release()
			if err != nil {
				return err
			}
			if connection == nil {
				direct := &forwardClientConn{conn: conn, identity: metadata.Pairs(
					"agentplane-sandbox-uid", destination.identity.UID,
					"agentplane-incarnation-uid", destination.podUID,
					"agentplane-runner-boot-id", response.RunnerBootId,
					"agentplane-connection-epoch", strconv.FormatUint(epoch, 10),
				)}
				connection = &runnerConnection{
					identity: destination.identity, podUID: destination.podUID, epoch: epoch,
					bootID: response.RunnerBootId, direct: direct, runner: runnerservicepb.NewRunnerClient(direct),
					ended: make(chan struct{}), closeDirect: cancel,
				}
				g.connections.accept(connection)
			}
			if !liveness.Stop() {
				select {
				case <-liveness.C:
				default:
				}
			}
			liveness.Reset(timing.LivenessTimeout)
		}
	}
}
