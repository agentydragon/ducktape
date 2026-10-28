package runner

import (
	"context"
	"errors"
	"sync"

	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/runner_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/runner/control"
	"github.com/agentydragon/ducktape/agentplane/runner/ingress"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/agentydragon/ducktape/agentplane/runner/session"
	"github.com/agentydragon/ducktape/agentplane/runner/stream"
)

func (r *Runner) Run(ctx context.Context, controlEndpoint, ingressEndpoint Endpoint) error {
	if err := r.Startup(r.ctx); err != nil {
		return errors.Join(err, r.Close())
	}
	controlClient, err := control.New(control.Dependencies{
		BootID: r.bootID,
		Server: r,
		AdoptIncarnation: func(_ context.Context, uid string) error {
			return r.adoptIncarnation(r.ctx, uid)
		},
		Facts:   r.facts,
		Changes: r.changesSince,
		StopRetry: func(err error) bool {
			var storage *journal.StorageError
			return errors.As(err, &storage)
		},
	})
	if err != nil {
		return errors.Join(err, r.Close())
	}
	streamCtx, cancel := context.WithCancel(context.Background())
	var streams sync.WaitGroup
	streams.Add(2)
	go func() {
		defer streams.Done()
		if err := controlClient.Run(streamCtx, control.Endpoint{Target: controlEndpoint.Target, Bearer: controlEndpoint.Bearer}); err != nil {
			r.fail(err)
		}
	}()
	go func() {
		defer streams.Done()
		if err := r.ingressLoop(streamCtx, ingressEndpoint); err != nil {
			r.fail(err)
		}
	}()
	var failure error
	select {
	case <-ctx.Done():
	case failure = <-r.fatal:
	}
	closeErr := r.Close()
	cancel()
	streams.Wait()
	return errors.Join(failure, closeErr)
}

func (r *Runner) ingressLoop(ctx context.Context, endpoint Endpoint) error {
	ctx, cancel := context.WithCancel(ctx)
	conn, err := stream.NewConnection(endpoint.Target)
	if err != nil {
		return err
	}
	defer conn.Close()
	client := ingress.NewClient(ingresspb.NewRunnerIngressClient(conn), r.store, func(id string, ack retained.Acknowledgement) error {
		if err := r.lifecycle.publishAck(id, ack); err != nil {
			return err
		}
		r.changed()
		return nil
	})
	authCtx := stream.Authenticated(ctx, endpoint.Bearer)
	active := map[string]bool{}
	finished := make(chan string, 16)
	var pushes sync.WaitGroup
	defer func() { cancel(); pushes.Wait() }()
	for {
		changed := r.changesSince()
		for _, s := range r.sessions.Inventory() {
			if active[s.ID()] {
				continue
			}
			ack, err := r.store.ReadAcknowledgement(s.ID())
			if err != nil {
				return err
			}
			if ack != nil && ack.Sealed {
				continue
			}
			active[s.ID()] = true
			pushes.Add(1)
			go func(s *session.Session) {
				defer pushes.Done()
				err := client.Deliver(authCtx, ingress.Source{ID: s.ID(), Announce: s.Attached, Journal: s.EventJournal()})
				if err != nil {
					r.fail(err)
				}
				select {
				case finished <- s.ID():
				case <-ctx.Done():
				}
			}(s)
		}
		select {
		case <-ctx.Done():
			return nil
		case id := <-finished:
			delete(active, id)
		case <-changed:
		}
	}
}
