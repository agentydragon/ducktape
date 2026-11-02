package harnesshost

import (
	"errors"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/ingress"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
)

// Startup recovers durable Session state before Runtime starts History delivery
// or exposes public control. The Host owns the Session component, while Runtime
// owns the startup worker and the environment lifetime.
func (r *Host) Startup(bridge RuntimeBridge) error {
	if bridge == nil {
		return errors.New("Harness Host requires its Runtime coordinator")
	}
	r.mu.Lock()
	r.runtimeBridge = bridge
	r.mu.Unlock()
	if err := r.sessions.Startup(r.ctx); err != nil {
		r.fail(err)
		return err
	}
	r.mu.Lock()
	r.startupComplete = true
	r.mu.Unlock()
	close(r.startupDone)
	return nil
}

// Failures reports a local Host failure to Runtime's supervision worker.
func (r *Host) Failures() <-chan error { return r.fatal }

// ReportFailure closes the Host admission gate before notifying Runtime.
func (r *Host) ReportFailure(err error) { r.fail(err) }

// IngressSupervisor supplies Session journals and the durable ACK store to a
// Runtime-owned delivery worker. Runtime receives local worker failures before
// it joins that worker, so admission can be fenced without waiting for siblings.
func (r *Host) IngressSupervisor(endpoint configuration.Endpoint, onWorkerFailure func(error)) *ingress.Supervisor {
	return ingress.NewSupervisor(ingress.SupervisorConfig{
		Target: endpoint.Target, Bearer: endpoint.Bearer, Store: r.store, Changes: r.changesSince, Inventory: r.ingressSources, OnWorkerFailure: onWorkerFailure,
		OnAcknowledgement: func(_ string, _ retained.Acknowledgement) error {
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
