package runner

import (
	"context"
	"errors"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/runner/configuration"
	"github.com/agentydragon/ducktape/agentplane/runner/ingress"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"google.golang.org/grpc"
)

func (r *Runner) Run(ctx context.Context, ingressEndpoint configuration.Endpoint) error {
	if err := r.Startup(r.ctx); err != nil {
		return errors.Join(err, r.Close(context.WithoutCancel(ctx)))
	}
	serverCtx, cancelServer := context.WithCancel(context.WithoutCancel(ctx))
	ingressCtx, cancelIngress := context.WithCancel(context.WithoutCancel(ctx))
	var streams sync.WaitGroup
	listener, server, err := r.control.Listen(r.execution)
	if err != nil {
		cancelServer()
		cancelIngress()
		return errors.Join(err, r.Close(context.WithoutCancel(ctx)))
	}
	streams.Add(1)
	go func() {
		defer streams.Done()
		if err := server.Serve(listener); err != nil && !errors.Is(err, grpc.ErrServerStopped) && serverCtx.Err() == nil {
			r.fail(err)
		}
	}()
	streams.Add(1)
	go func() {
		defer streams.Done()
		if err := r.newIngressSupervisor(ingressEndpoint).Run(ingressCtx); err != nil {
			r.fail(err)
		}
	}()
	var failure error
	select {
	case <-ctx.Done():
	case failure = <-r.fatal:
	}
	closeErr := r.Close(context.WithoutCancel(ctx))
	cancelServer()
	cancelIngress()
	server.Stop()
	_ = listener.Close()
	streams.Wait()
	return errors.Join(failure, closeErr)
}

func (r *Runner) newIngressSupervisor(endpoint configuration.Endpoint) *ingress.Supervisor {
	return ingress.NewSupervisor(ingress.SupervisorConfig{
		Target:          endpoint.Target,
		Bearer:          endpoint.Bearer,
		Store:           r.store,
		Changes:         r.changesSince,
		Inventory:       r.ingressSources,
		OnWorkerFailure: r.fail,
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
	currentSessions := r.sessions.Inventory()
	sources := make([]ingress.Source, 0, len(currentSessions))
	for _, current := range currentSessions {
		sources = append(sources, ingress.Source{ID: current.ID(), Announce: current.Attached, Journal: current.EventJournal()})
	}
	return sources
}
