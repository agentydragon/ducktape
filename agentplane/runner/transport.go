package runner

import (
	"context"
	"errors"
	"sync"

	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/runner_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/runner/configuration"
	"github.com/agentydragon/ducktape/agentplane/runner/ingress"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/agentydragon/ducktape/agentplane/runner/session"
	"github.com/agentydragon/ducktape/agentplane/runner/stream"
	"google.golang.org/grpc"
)

func (r *Runner) Run(ctx context.Context, ingressEndpoint configuration.Endpoint) error {
	if err := r.Startup(r.ctx); err != nil {
		return errors.Join(err, r.Close(context.WithoutCancel(ctx)))
	}
	serverCtx, cancelServer := context.WithCancel(context.WithoutCancel(ctx))
	ingressCtx, cancelIngress := context.WithCancel(context.WithoutCancel(ctx))
	var streams sync.WaitGroup
	listener, server, err := r.control.Listen(r.executorRelay)
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
		if err := r.ingressLoop(ingressCtx, ingressEndpoint); err != nil {
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

func (r *Runner) ingressLoop(ctx context.Context, endpoint configuration.Endpoint) error {
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
