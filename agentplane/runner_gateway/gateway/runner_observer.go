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
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"github.com/sethvargo/go-retry"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	toolscache "k8s.io/client-go/tools/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var errRunnerOwned = status.Error(codes.Unavailable, connectionHeldByAnotherReplica)

type fencedRunnerConn struct {
	conn     *grpc.ClientConn
	identity metadata.MD
}

func (c *fencedRunnerConn) Invoke(ctx context.Context, method string, args, reply any, opts ...grpc.CallOption) error {
	return c.conn.Invoke(metadata.NewOutgoingContext(ctx, c.identity.Copy()), method, args, reply, opts...)
}

func (c *fencedRunnerConn) NewStream(ctx context.Context, desc *grpc.StreamDesc, method string, opts ...grpc.CallOption) (grpc.ClientStream, error) {
	return c.conn.NewStream(metadata.NewOutgoingContext(ctx, c.identity.Copy()), desc, method, opts...)
}

// runnerObserver is one Sandbox's observer worker; done closes when the worker stops on its own.
type runnerObserver struct {
	destination runnerDestination
	cancel      context.CancelFunc
	done        chan struct{}
}

// RunRunnerObservers keeps one observer per reachable Runner, reconciling each Sandbox that
// RunnerDiscoveryHandler enqueues against the Gateway's reader (the informer cache in production), so
// no Kubernetes read repeats on a timer. The existing Lease elects one Gateway replica; connection
// availability never chooses command queue ownership.
func (g *Gateway) RunRunnerObservers(ctx context.Context) error {
	active := make(map[string]runnerObserver)
	var workers sync.WaitGroup
	defer func() {
		for _, observer := range active {
			observer.cancel()
		}
		workers.Wait()
	}()
	go func() {
		<-ctx.Done()
		g.observerQueue.ShutDown()
	}()
	for {
		name, shutdown := g.observerQueue.Get()
		if shutdown {
			return ctx.Err()
		}
		if err := g.reconcileObserver(ctx, name, active, &workers); err != nil {
			slog.Warn("runner observer reconcile failed", "sandbox", name, "error", err)
			g.observerQueue.AddRateLimited(name)
		} else {
			g.observerQueue.Forget(name)
		}
		g.observerQueue.Done(name)
	}
}

// reconcileObserver starts, replaces or stops the named Sandbox's observer to match its current runner.
// A verdict that the Sandbox has no reachable runner stops the observer until the Sandbox's next event;
// a failed read is retried with backoff.
func (g *Gateway) reconcileObserver(ctx context.Context, name string, active map[string]runnerObserver, workers *sync.WaitGroup) error {
	// A KubeVirt informer starts on its first read and blocks it until synced, so reads are bounded.
	readCtx, release := context.WithTimeout(ctx, g.config.ControlTiming.LivenessTimeout)
	defer release()
	var wanted *runnerDestination
	var sandbox sandboxapi.Sandbox
	err := g.objects.Get(readCtx, client.ObjectKey{Namespace: g.config.Namespace, Name: name}, &sandbox)
	switch {
	case apierrors.IsNotFound(err):
	case err != nil:
		return fmt.Errorf("read Sandbox: %w", err)
	default:
		destination, err := g.runnerDestination(readCtx, &sandbox)
		if _, verdict := status.FromError(err); !verdict {
			return err
		}
		if err == nil {
			wanted = &destination
		}
	}
	if current, exists := active[name]; exists {
		select {
		case <-current.done:
		default:
			if wanted != nil && current.destination == *wanted {
				return nil
			}
			current.cancel()
		}
		delete(active, name)
	}
	if wanted == nil {
		return nil
	}
	destination := *wanted
	workerCtx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	active[name] = runnerObserver{destination, cancel, done}
	workers.Add(1)
	go func() {
		defer workers.Done()
		defer close(done)
		defer cancel()
		err := retry.Do(workerCtx, retry.NewConstant(time.Second), func(ctx context.Context) error {
			err := g.observeRunner(ctx, destination)
			if err == nil || ctx.Err() != nil || errors.Is(err, errRunnerOwned) {
				return err
			}
			// Transport recovery does not wait for a Sandbox or Pod event.
			// Each attempt claims a fresh epoch before accepting the new boot.
			slog.Warn("runner observer reconnecting", "sandbox", destination.identity.Name, "error", err)
			return retry.RetryableError(err)
		})
		if errors.Is(err, errRunnerOwned) {
			// Another replica holds the runner's Lease. Its lapse changes nothing in the cache, so the
			// claim is retried on the heartbeat cadence.
			g.observerQueue.AddAfter(name, g.config.ControlTiming.HeartbeatInterval)
		}
	}()
	return nil
}

// RunnerDiscoveryHandler enqueues the Sandbox an event concerns for RunRunnerObservers: the Sandbox
// itself, or the Sandbox a runner Pod is labelled with.
func (g *Gateway) RunnerDiscoveryHandler() toolscache.ResourceEventHandler {
	enqueue := func(obj any) {
		if name, ok := observedSandbox(obj); ok {
			g.observerQueue.Add(name)
		}
	}
	return toolscache.ResourceEventHandlerFuncs{
		AddFunc:    enqueue,
		UpdateFunc: func(_, obj any) { enqueue(obj) },
		DeleteFunc: enqueue,
	}
}

// observedSandbox names the Sandbox whose runner obj can change; a Pod without the Sandbox label
// concerns none.
func observedSandbox(obj any) (string, bool) {
	if tombstone, ok := obj.(toolscache.DeletedFinalStateUnknown); ok {
		obj = tombstone.Obj
	}
	switch object := obj.(type) {
	case *sandboxapi.Sandbox:
		return object.Name, true
	case *corev1.Pod:
		name := object.Labels[sandboxapi.SandboxLabel]
		return name, name != ""
	}
	return "", false
}

type observedState struct {
	response *runnerservicepb.WatchStateResponse
	err      error
}

func (g *Gateway) observeRunner(ctx context.Context, destination runnerDestination) error {
	timing := g.config.ControlTiming
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	creds, err := g.config.RunnerSecurity.ClientCredentials(destination.peerURI)
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
	epoch, err := g.records.claim(claimCtx, destination.identity, destination.podUID)
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
	var connection *runnerConnection
	defer func() {
		if connection != nil {
			g.connections.remove(connection, status.Error(codes.Unavailable, "runner observation ended"))
		}
	}()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-liveness.C:
			return status.Error(codes.Unavailable, "runner state timed out")
		case received := <-responses:
			if received.err != nil {
				return received.err
			}
			response := received.response
			if err := g.config.RunnerSecurity.AuthorizePeer(watch.Context(), destination.peerURI); err != nil {
				return status.Error(codes.Unauthenticated, "runner certificate is no longer valid")
			}
			if response.GetRunnerBootId() == "" || response.GetSandboxUid() != destination.identity.UID || response.GetIncarnationUid() != destination.podUID || response.GetConnectionEpoch() != epoch || response.GetFacts() == nil {
				return status.Error(codes.InvalidArgument, "runner state has invalid identity or facts")
			}
			if connection != nil && connection.bootID != response.RunnerBootId {
				return status.Error(codes.Aborted, "runner boot changed")
			}
			facts, err := sandboxlease.FromProto(response.GetFacts())
			if err != nil {
				return status.Error(codes.InvalidArgument, "runner sent invalid facts")
			}
			validateCtx, release := context.WithTimeout(ctx, timing.LivenessTimeout)
			err = g.validateRunnerDestination(validateCtx, destination)
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
				direct := &fencedRunnerConn{conn: conn, identity: metadata.Pairs(
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
