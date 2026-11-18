package runtimeproxy

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/observability"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/sethvargo/go-retry"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	toolscache "k8s.io/client-go/tools/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type fencedRuntimeConn struct {
	conn       *grpc.ClientConn
	identity   metadata.MD
	connection *runtimeConnection
}

// Invoke reports a call its connection's end cancelled (conn closing under a call that held it, or
// the executor's end-of-connection cancel) as that end, which callers retry, rather than Canceled.
func (c *fencedRuntimeConn) Invoke(ctx context.Context, method string, args, reply any, opts ...grpc.CallOption) error {
	err := c.conn.Invoke(metadata.NewOutgoingContext(ctx, c.identity.Copy()), method, args, reply, opts...)
	if status.Code(err) == codes.Canceled {
		select {
		case <-c.connection.ended:
			return c.connection.error()
		default:
		}
	}
	return err
}

func (c *fencedRuntimeConn) NewStream(ctx context.Context, desc *grpc.StreamDesc, method string, opts ...grpc.CallOption) (grpc.ClientStream, error) {
	streamContext, cancel := context.WithCancel(ctx)
	go func() {
		select {
		case <-c.connection.ended:
			cancel()
		case <-streamContext.Done():
		}
	}()
	stream, err := c.conn.NewStream(metadata.NewOutgoingContext(streamContext, c.identity.Copy()), desc, method, opts...)
	if err != nil {
		cancel()
		return nil, err
	}
	return stream, nil
}

// runtimeObserver is one Sandbox's observer worker; done closes when the worker stops on its own.
type runtimeObserver struct {
	destination runtimeDestination
	cancel      context.CancelFunc
	done        chan struct{}
}

// RunRuntimeObservers keeps one observer per reachable Environment Runtime, reconciling each Sandbox that
// RuntimeDiscoveryHandler enqueues against the Server's reader (the informer cache in production), so
// no Kubernetes read repeats on a timer. Every replica observes independently; connection
// availability never chooses command queue ownership.
func (g *Server) RunRuntimeObservers(ctx context.Context) error {
	active := make(map[string]runtimeObserver)
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
			slog.Warn("Environment Runtime observer reconcile failed", "sandbox", name, "error", err)
			g.observerQueue.AddRateLimited(name)
		} else {
			g.observerQueue.Forget(name)
		}
		g.observerQueue.Done(name)
	}
}

// reconcileObserver starts, replaces or stops the named Sandbox's observer to match its current Environment Runtime.
// A verdict that the Sandbox has no reachable Environment Runtime stops the observer until the Sandbox's next event;
// a failed read is retried with backoff.
func (g *Server) reconcileObserver(ctx context.Context, name string, active map[string]runtimeObserver, workers *sync.WaitGroup) error {
	// A KubeVirt informer starts on its first read and blocks it until synced, so reads are bounded.
	readCtx, release := context.WithTimeout(ctx, g.config.ControlTiming.LivenessTimeout)
	defer release()
	var wanted *runtimeDestination
	var sandbox sandboxapi.Sandbox
	err := g.objects.Get(readCtx, client.ObjectKey{Namespace: g.config.Namespace, Name: name}, &sandbox)
	switch {
	case apierrors.IsNotFound(err):
	case err != nil:
		return fmt.Errorf("read Sandbox: %w", err)
	default:
		destination, err := g.runtimeDestination(readCtx, &sandbox)
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
			if wanted != nil && current.destination.equal(*wanted) {
				return nil
			}
			current.cancel()
			// Complete the old worker before starting its successor: a delayed snapshot
			// from a canceled observer must not replace the new local connection.
			select {
			case <-current.done:
			case <-readCtx.Done():
				return readCtx.Err()
			}
		}
		delete(active, name)
	}
	if wanted == nil {
		return nil
	}
	destination := *wanted
	workerCtx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	active[name] = runtimeObserver{destination, cancel, done}
	workers.Add(1)
	go func() {
		defer workers.Done()
		defer close(done)
		defer cancel()
		_ = retry.Do(workerCtx, retry.NewConstant(time.Second), func(ctx context.Context) error {
			err := g.observeRuntime(ctx, destination)
			if err == nil || ctx.Err() != nil {
				return err
			}
			// Transport recovery does not wait for a Sandbox or Pod event.
			// Each attempt validates the current workload and learns its boot ID.
			slog.Warn("Environment Runtime observer reconnecting", "sandbox", destination.identity.Name, "error", err)
			return retry.RetryableError(err)
		})
	}()
	return nil
}

// RuntimeDiscoveryHandler enqueues the Sandbox an event concerns for RunRuntimeObservers: the Sandbox
// itself, or the Sandbox an Environment Runtime Pod is labelled with.
func (g *Server) RuntimeDiscoveryHandler() toolscache.ResourceEventHandler {
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

// observedSandbox names the Sandbox whose Environment Runtime workload can change; a Pod without the Sandbox label
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

func (g *Server) observeRuntime(ctx context.Context, destination runtimeDestination) error {
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
	var connection *runtimeConnection
	defer func() {
		if connection != nil {
			g.connections.remove(connection, status.Error(codes.Unavailable, "Environment Runtime observation ended"))
		}
		cancel()
		_ = conn.Close()
	}()
	watch, err := environmentruntimepb.NewEnvironmentRuntimeClient(conn).WatchState(ctx, &environmentruntimepb.WatchStateRequest{
		SandboxUid: destination.identity.Uid, IncarnationUid: destination.podUID,
	})
	if err != nil {
		return err
	}
	span := trace.SpanFromContext(watch.Context())
	span.SetAttributes(
		attribute.String("sandbox.name", destination.identity.Name),
		attribute.String("sandbox.uid", destination.identity.Uid),
	)
	return streaming.ReceiveWithHeartbeat(ctx, timing.LivenessTimeout,
		status.Error(codes.Unavailable, "Environment Runtime state timed out"), watch.Recv, nil,
		func(response *environmentruntimepb.WatchStateResponse) error {
			if err := g.config.RunnerSecurity.AuthorizePeer(watch.Context(), destination.peerURI); err != nil {
				return status.Error(codes.Unauthenticated, "Environment Runtime certificate is no longer valid")
			}
			if response.GetEnvironmentRuntimeBootId() == "" || response.GetSandboxUid() != destination.identity.Uid || response.GetIncarnationUid() != destination.podUID {
				return status.Error(codes.InvalidArgument, "Environment Runtime state has invalid identity")
			}
			if connection != nil && connection.bootID != response.EnvironmentRuntimeBootId {
				return status.Error(codes.Aborted, "Environment Runtime boot changed")
			}
			validateCtx, release := context.WithTimeout(ctx, timing.LivenessTimeout)
			err := g.validateRuntimeDestination(validateCtx, destination)
			if err == nil && connection == nil {
				connection = &runtimeConnection{
					identity: destination.identity, podUID: destination.podUID,
					bootID: response.EnvironmentRuntimeBootId, ended: make(chan struct{}), closeDirect: cancel,
				}
				connection.direct = &fencedRuntimeConn{conn: conn, connection: connection, identity: metadata.Pairs(
					"agentplane-sandbox-uid", destination.identity.Uid,
					"agentplane-incarnation-uid", destination.podUID,
					"agentplane-environment-runtime-boot-id", response.EnvironmentRuntimeBootId,
				)}
				connection.runtime = environmentruntimepb.NewEnvironmentRuntimeClient(connection.direct)
				connection.harnessHost = harnesshostpb.NewHarnessHostClient(connection.direct)
				// Publish the callable connection only after validating the workload and boot.
				g.connections.accept(connection)
				span.AddEvent("runner.control.ready", trace.WithAttributes(
					attribute.String("sandbox.incarnation_uid", destination.podUID),
					attribute.String("runtime.boot_id", response.EnvironmentRuntimeBootId),
				))
			}
			release()
			return err
		})
}
