package harnesshost

import (
	"context"
	"errors"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/ingress"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/retained"
)

// Run joins the embedded Host controllers under Runtime's management lifetime.
// Only the public Gateway control server remains a network listener.
func (r *Host) Run(ctx context.Context, endpoint configuration.Endpoint, bridge RuntimeBridge) error {
	if bridge == nil {
		return errors.New("Harness Host requires its Runtime coordinator")
	}
	r.mu.Lock()
	r.runtimeBridge = bridge
	r.mu.Unlock()
	if err := r.sessions.Startup(r.ctx); err != nil {
		return errors.Join(err, r.Close(context.WithoutCancel(ctx)))
	}
	r.mu.Lock()
	r.startupComplete = true
	r.mu.Unlock()
	servingCtx, cancel := context.WithCancel(context.WithoutCancel(ctx))
	defer cancel()
	var workers sync.WaitGroup
	workers.Go(func() {
		if err := r.newIngressSupervisor(endpoint).Run(servingCtx); err != nil && servingCtx.Err() == nil {
			r.fail(err)
		}
	})
	workers.Go(func() {
		err := bridge.ConnectHost(servingCtx, r)
		if servingCtx.Err() == nil {
			r.fail(errors.Join(errors.New("Runtime Host coordinator ended"), err))
		}
	})
	var failure error
	select {
	case <-ctx.Done():
	case failure = <-r.fatal:
	}
	// Accepted native effects and their journals settle before the coordinator
	// is canceled; Runtime waits for this joined lifetime before releasing its lock.
	closeErr := r.Close(context.WithoutCancel(ctx))
	cancel()
	workers.Wait()
	return errors.Join(failure, closeErr)
}

func (r *Host) newIngressSupervisor(endpoint configuration.Endpoint) *ingress.Supervisor {
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

func (r *Host) ingressSources() []ingress.Source {
	current := r.sessions.Inventory()
	sources := make([]ingress.Source, 0, len(current))
	for _, s := range current {
		sources = append(sources, ingress.Source{ID: s.ID(), Announce: s.Attached, Journal: s.EventJournal()})
	}
	return sources
}
