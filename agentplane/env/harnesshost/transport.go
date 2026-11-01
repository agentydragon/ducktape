package harnesshost

import (
	"context"
	"errors"
	"net"
	"os"
	"path/filepath"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/ingress"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/retained"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"google.golang.org/grpc"
)

func (r *Runner) Run(ctx context.Context, endpoint configuration.Endpoint) error {
	if err := r.sessions.Startup(r.ctx); err != nil {
		return errors.Join(err, r.Close(context.WithoutCancel(ctx)))
	}
	if err := os.MkdirAll(filepath.Dir(r.config.ListenSocket), 0700); err != nil {
		return errors.Join(err, r.Close(context.WithoutCancel(ctx)))
	}
	if info, err := os.Lstat(r.config.ListenSocket); err == nil {
		if info.Mode()&os.ModeSocket == 0 {
			return errors.Join(errors.New("host control path is not a socket"), r.Close(context.WithoutCancel(ctx)))
		}
		if err := os.Remove(r.config.ListenSocket); err != nil {
			return errors.Join(err, r.Close(context.WithoutCancel(ctx)))
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return errors.Join(err, r.Close(context.WithoutCancel(ctx)))
	}
	listener, err := net.Listen("unix", r.config.ListenSocket)
	if err != nil {
		return errors.Join(err, r.Close(context.WithoutCancel(ctx)))
	}
	defer listener.Close()
	if err := os.Chmod(r.config.ListenSocket, 0600); err != nil {
		return errors.Join(err, r.Close(context.WithoutCancel(ctx)))
	}
	server := grpc.NewServer(observability.GRPCServerOption(), grpc.UnaryInterceptor(func(ctx context.Context, request any, _ *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		r.mu.Lock()
		err := r.authorizeLocked(ctx)
		r.mu.Unlock()
		if err != nil {
			return nil, err
		}
		return handler(ctx, request)
	}))
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, r)
	defer server.Stop()
	servingCtx, cancel := context.WithCancel(context.WithoutCancel(ctx))
	defer cancel()
	var workers sync.WaitGroup
	workers.Go(func() {
		if err := server.Serve(listener); err != nil && !errors.Is(err, grpc.ErrServerStopped) {
			r.fail(err)
		}
	})
	workers.Go(func() {
		if err := r.newIngressSupervisor(endpoint).Run(servingCtx); err != nil && servingCtx.Err() == nil {
			r.fail(err)
		}
	})
	workers.Go(func() {
		if err := r.connectRuntime(servingCtx); err != nil && servingCtx.Err() == nil {
			r.fail(err)
		}
	})
	var failure error
	select {
	case <-ctx.Done():
	case failure = <-r.fatal:
	}
	// Close admission before disconnecting the protocol; the native controller
	// settles accepted effects and joins readers before journals and locks close.
	closeErr := r.Close(context.WithoutCancel(ctx))
	cancel()
	server.Stop()
	_ = listener.Close()
	workers.Wait()
	return errors.Join(failure, closeErr)
}

func (r *Runner) newIngressSupervisor(endpoint configuration.Endpoint) *ingress.Supervisor {
	return ingress.NewSupervisor(ingress.SupervisorConfig{
		Target: endpoint.Target, Bearer: endpoint.Bearer, Store: r.store, Changes: r.changesSince, Inventory: r.ingressSources, OnWorkerFailure: r.fail,
		OnAcknowledgement: func(id string, ack retained.Acknowledgement) error {
			if err := r.lifecycle.publishAck(id, ack); err != nil {
				return err
			}
			r.changed()
			return nil
		},
	})
}

func (r *Runner) ingressSources() []ingress.Source {
	current := r.sessions.Inventory()
	sources := make([]ingress.Source, 0, len(current))
	for _, s := range current {
		sources = append(sources, ingress.Source{ID: s.ID(), Announce: s.Attached, Journal: s.EventJournal()})
	}
	return sources
}
