package harnesshost

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/session"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/env/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const (
	runtimeBootMetadata = "agentplane-runtime-boot-id"
	hostBootMetadata    = "agentplane-harness-host-boot-id"
	generationMetadata  = "agentplane-environment-generation"
)

func (r *Runner) environment() session.Environment {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.intent == nil {
		return session.Environment{Quiesced: true}
	}
	return session.Environment{IncarnationUID: r.intent.IncarnationUid, Quiesced: r.intent.Mode != runtimepb.LifecycleIntent_RUNNING}
}

// Caller holds the synchronous native admission barrier. The interceptor alone
// cannot fence a request that spent time waiting on model lookup or creation.
func (r *Runner) authorizeLocked(ctx context.Context) error {
	if r.closing || !r.ready.Load() || r.intent == nil {
		return status.Error(codes.Unavailable, "Harness Host has no ready environment binding")
	}
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return status.Error(codes.Unauthenticated, "environment binding is required")
	}
	binding := r.intent.Binding
	for key, want := range map[string]string{runtimeBootMetadata: binding.RuntimeBootId, hostBootMetadata: r.bootID, generationMetadata: strconv.FormatUint(binding.Generation, 10)} {
		values := md.Get(key)
		if len(values) != 1 || values[0] != want {
			return status.Error(codes.FailedPrecondition, "stale environment binding")
		}
	}
	return nil
}

func (r *Runner) applyIntent(ctx context.Context, intent *runtimepb.LifecycleIntent) error {
	if intent == nil || intent.Binding == nil || intent.Binding.RuntimeBootId == "" || intent.Binding.HostBootId != r.bootID || intent.Binding.Generation == 0 || intent.SandboxUid != r.config.Launch.Sandbox.UID {
		return errors.New("invalid environment lifecycle binding")
	}
	mode := runnerlifecycle.Quiescing
	switch intent.Mode {
	case runtimepb.LifecycleIntent_INACTIVE:
		if intent.IncarnationUid != "" || intent.SealUid != "" {
			return errors.New("inactive environment carries an incarnation")
		}
	case runtimepb.LifecycleIntent_RUNNING:
		if intent.IncarnationUid == "" || intent.SealUid != "" {
			return errors.New("running environment has invalid incarnation or seal")
		}
		mode = runnerlifecycle.Running
	case runtimepb.LifecycleIntent_QUIESCING:
		if intent.IncarnationUid == "" || intent.SealUid != intent.IncarnationUid {
			return errors.New("draining environment has no incarnation")
		}
	case runtimepb.LifecycleIntent_CLOSING:
		if intent.SealUid != "" && intent.SealUid != intent.IncarnationUid {
			return errors.New("closing environment seal does not match incarnation")
		}
	default:
		return errors.New("unknown environment lifecycle mode")
	}
	r.mu.Lock()
	if r.closing {
		r.mu.Unlock()
		return errors.New("Harness Host is closing")
	}
	previous := r.intent
	if previous != nil {
		if intent.Binding.RuntimeBootId != previous.Binding.RuntimeBootId || intent.Binding.Generation < previous.Binding.Generation {
			r.mu.Unlock()
			return errors.New("environment lifecycle binding regressed")
		}
		if intent.Binding.Generation == previous.Binding.Generation {
			old := proto.Clone(previous).(*runtimepb.LifecycleIntent)
			old.ExecutionFenced = intent.ExecutionFenced
			if !proto.Equal(old, intent) || previous.ExecutionFenced && !intent.ExecutionFenced {
				r.mu.Unlock()
				return errors.New("environment lifecycle intent changed without a new generation")
			}
		}
	}
	changedGeneration := previous == nil || intent.Binding.Generation != previous.Binding.Generation
	if changedGeneration && previous != nil && (previous.Mode != runtimepb.LifecycleIntent_RUNNING || previous.IncarnationUid != intent.IncarnationUid) && intent.Mode == runtimepb.LifecycleIntent_RUNNING && !r.nativeStopped {
		r.mu.Unlock()
		return errors.New("environment reopened before native drain completed")
	}
	r.ready.Store(false)
	r.intent = proto.Clone(intent).(*runtimepb.LifecycleIntent)
	if mode == runnerlifecycle.Running {
		r.bootstrapBinding = proto.Clone(intent.Binding).(*runtimepb.Binding)
	}
	r.mu.Unlock()
	if changedGeneration && mode == runnerlifecycle.Running {
		if err := r.sessions.ResetDemand(); err != nil {
			return err
		}
		if r.codexAppServer != nil {
			if err := r.codexAppServer.ResetDemand(ctx); err != nil {
				return err
			}
		}
		for _, s := range r.sessions.Inventory() {
			if err := r.sessions.Reopen(ctx, s); err != nil {
				return err
			}
			if err := r.sessions.ResumeRecovered(s); err != nil {
				return err
			}
		}
	}
	r.mu.Lock()
	if changedGeneration {
		r.nativeStopped = false
	}
	err := runnerlifecycle.PublishDesired(r.ctx, r.lifecycle.state, runnerlifecycle.Desired{Mode: mode, SealUID: intent.SealUid, Generation: intent.Binding.Generation})
	if err == nil {
		input, _, readErr := lifecycleRead[runnerInputSpec](r.ctx, r.lifecycle.state, "runner")
		if readErr != nil {
			err = readErr
		} else {
			input.ExecutionFenced = intent.ExecutionFenced
			err = publishLifecycle(r.lifecycle, "runner", input)
		}
	}
	if err == nil {
		r.ready.Store(true)
	}
	r.mu.Unlock()
	r.changed()
	return err
}

func (r *Runner) waitBootstrap(ctx context.Context) error {
	r.mu.Lock()
	if r.bootstrapBinding == nil || r.runtimeClient == nil {
		r.mu.Unlock()
		return errors.New("environment runtime is not connected")
	}
	binding := proto.Clone(r.bootstrapBinding).(*runtimepb.Binding)
	client := r.runtimeClient
	r.mu.Unlock()
	_, err := client.WaitBootstrap(ctx, binding)
	return err
}

func (r *Runner) executionReady(ctx context.Context) (bool, error) {
	if r.config.ExecServerURL == "" {
		return true, nil
	}
	if r.config.ExecutionSocket == "" {
		return false, errors.New("execution socket is required for a separate exec-server")
	}
	conn, err := grpc.NewClient("unix://"+r.config.ExecutionSocket, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return false, err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	result, err := grpc_health_v1.NewHealthClient(conn).Check(ctx, &grpc_health_v1.HealthCheckRequest{})
	if err != nil {
		if status.Code(err) == codes.Unavailable || status.Code(err) == codes.DeadlineExceeded {
			return false, nil
		}
		return false, err
	}
	return result.Status == grpc_health_v1.HealthCheckResponse_SERVING, nil
}

// A connection is one Runtime incarnation. Losing it ends this Host process;
// neither mutations nor a new incarnation are retried on an uncertain channel.
func (r *Runner) connectRuntime(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	conn, err := grpc.NewClient("unix://"+r.config.RuntimeSocket, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return err
	}
	defer conn.Close()
	client := runtimepb.NewEnvironmentRuntimeClient(conn)
	r.mu.Lock()
	r.runtimeClient = client
	r.mu.Unlock()
	stream, err := client.Connect(ctx)
	if err != nil {
		return err
	}
	if err := stream.Send(&runtimepb.ConnectRequest{Message: &runtimepb.ConnectRequest_Hello{Hello: &runtimepb.HostHello{HostBootId: r.bootID, ControlSocket: r.config.ListenSocket}}}); err != nil {
		return err
	}
	failures := make(chan error, 1)
	go func() {
		for {
			intent, err := stream.Recv()
			if err == nil {
				err = r.applyIntent(ctx, intent)
			}
			if err != nil {
				failures <- err
				return
			}
		}
	}()
	for {
		changed := r.changesSince()
		r.mu.Lock()
		var sampledBinding *runtimepb.Binding
		if r.intent != nil {
			sampledBinding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
		}
		r.mu.Unlock()
		facts, err := r.facts()
		if err != nil {
			return err
		}
		r.mu.Lock()
		if !proto.Equal(sampledBinding, r.intent.GetBinding()) {
			r.mu.Unlock()
			continue
		}
		var report *runtimepb.HostReport
		if r.intent != nil {
			report = &runtimepb.HostReport{Binding: proto.Clone(r.intent.Binding).(*runtimepb.Binding), Ready: r.ready.Load(), NativeStopped: r.nativeStopped, Facts: facts}
			current := r.lifecycle.status()
			if current.Generation == r.intent.Binding.Generation {
				report.Error = current.Error
			}
		}
		r.mu.Unlock()
		if report != nil {
			if err := stream.Send(&runtimepb.ConnectRequest{Message: &runtimepb.ConnectRequest_Report{Report: report}}); err != nil {
				return err
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-failures:
			return fmt.Errorf("environment control ended: %w", err)
		case <-changed:
		}
	}
}
