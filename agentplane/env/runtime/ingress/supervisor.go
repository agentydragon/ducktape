package ingress

import (
	"context"
	"errors"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/stream"
	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/api/ingress/session_ingresspb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

// SupervisorConfig supplies the Runtime-owned Session inventory and observation
// seams used by the per-Session ingress workers.
type SupervisorConfig struct {
	Target            string
	Bearer            string
	Transport         credentials.TransportCredentials
	ProxyAddress      string
	Store             *retained.Store
	Inventory         func() []Source
	InventoryChanges  func() <-chan struct{}
	OnAcknowledgement func(string, retained.Acknowledgement) error
	OnWorkerFailure   func(error)
}

// Supervisor discovers unsealed Session histories and keeps one delivery
// worker for each until it seals or fails.
type Supervisor struct {
	config SupervisorConfig
}

func NewSupervisor(config SupervisorConfig) *Supervisor {
	return &Supervisor{config: config}
}

// Run keeps delivery workers alive until cancellation or a supervisor failure.
// Worker failures are reported while the failed Session remains eligible for
// retry. Callers decide when shutdown may cancel ingress so final ACKs can
// still satisfy Runtime quiescence.
func (s *Supervisor) Run(ctx context.Context) error {
	if s == nil || s.config.Store == nil || s.config.Inventory == nil || s.config.InventoryChanges == nil || s.config.OnWorkerFailure == nil {
		return errors.New("ingress supervisor requires a store, inventory, change source, and worker failure handler")
	}
	var conn *grpc.ClientConn
	var err error
	if s.config.Transport != nil {
		conn, err = stream.NewTunneledConnection(s.config.Target, s.config.ProxyAddress, s.config.Transport)
	} else {
		conn, err = stream.NewConnection(s.config.Target)
	}
	if err != nil {
		return err
	}
	defer conn.Close()

	client := NewClient(ingresspb.NewSessionJournalIngressClient(conn), s.config.Store, s.config.OnAcknowledgement)
	return s.supervise(ctx, func(ctx context.Context, source Source) error {
		if s.config.Transport == nil {
			ctx = stream.Authenticated(ctx, s.config.Bearer)
		}
		return client.Deliver(ctx, source)
	})
}

type deliveryResult struct {
	id  string
	err error
}

func (s *Supervisor) supervise(ctx context.Context, deliver func(context.Context, Source) error) error {
	ctx, cancel := context.WithCancel(ctx)
	var workers sync.WaitGroup
	defer func() {
		cancel()
		workers.Wait()
	}()

	active := make(map[string]struct{})
	finished := make(chan deliveryResult, 16)
	for {
		if ctx.Err() != nil {
			return nil
		}
		changed := s.config.InventoryChanges()
		for _, source := range s.config.Inventory() {
			if _, found := active[source.ID]; found {
				continue
			}
			ack, err := s.config.Store.ReadAcknowledgement(source.ID)
			if err != nil {
				return err
			}
			if ack != nil && ack.Sealed {
				continue
			}

			active[source.ID] = struct{}{}
			workers.Add(1)
			go func(source Source) {
				defer workers.Done()
				err := deliver(ctx, source)
				select {
				case finished <- deliveryResult{id: source.ID, err: err}:
				case <-ctx.Done():
				}
			}(source)
		}

		select {
		case <-ctx.Done():
			return nil
		case result := <-finished:
			delete(active, result.id)
			if result.err != nil && ctx.Err() == nil {
				s.config.OnWorkerFailure(result.err)
			}
		case <-changed:
		}
	}
}
