package executorrelay

import (
	"context"
	"strconv"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/runner/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/runner/control"
	runnerexecpb "github.com/agentydragon/ducktape/agentplane/runner/runner_execpb"
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	"github.com/cosi-project/runtime/pkg/state"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type Authority interface {
	Authorize(context.Context) (*control.Epoch, error)
	Validate(*control.Epoch) error
}

type Config struct {
	Context   context.Context
	Authority Authority
	Executor  *codexexecserver.Host
	State     state.State
	Admit     func(func() error) error
	OnChange  func()
	WaitFor   func(context.Context, func() (bool, error)) error
	IsRunning func() bool
}

type Manager struct {
	runnerexecpb.UnimplementedRunnerExecServer
	config   Config
	mu       sync.Mutex
	relays   map[string]*relay
	streams  int
	sequence uint64
}

var _ runnerexecpb.RunnerExecServer = (*Manager)(nil)

func New(config Config) *Manager {
	return &Manager{config: config, relays: make(map[string]*relay)}
}

// Each direct executor stream owns its cancellation scope, relay and workers.
type Connection struct {
	manager  *Manager
	ctx      context.Context
	cancel   context.CancelFunc
	id       string
	opened   bool
	send     func(*executorpb.ExecutorFrame) error
	sendData func(*executorpb.ExecutorFrame) error
	workers  sync.WaitGroup
	close    sync.Once
}

func (m *Manager) Open(ctx context.Context, epoch uint64, send, sendData func(*executorpb.ExecutorFrame) error) (*Connection, error) {
	m.mu.Lock()
	if m.streams >= maxExecutorStreamsPerRunner {
		m.mu.Unlock()
		return nil, status.Error(codes.ResourceExhausted, "Runner has too many direct executor streams")
	}
	m.streams++
	m.sequence++
	sequence := m.sequence
	m.mu.Unlock()
	ctx, cancel := context.WithCancel(ctx)
	id := strconv.FormatUint(epoch, 10) + "-" + strconv.FormatUint(sequence, 10)
	return &Connection{manager: m, ctx: ctx, cancel: cancel, id: id, send: send, sendData: sendData}, nil
}

func (c *Connection) Route(frame *executorpb.ExecutorFrame) error {
	return c.manager.routeFrame(c.ctx, frame, c.id, &c.opened, c.send, c.sendData, &c.workers)
}

func (c *Connection) Close() {
	c.close.Do(func() {
		c.cancel()
		c.workers.Wait()
		c.manager.mu.Lock()
		c.manager.streams--
		c.manager.mu.Unlock()
	})
}

func (m *Manager) Active() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.relays) > 0
}

func (m *Manager) Drain(ctx context.Context) error {
	m.mu.Lock()
	relays := make([]*relay, 0, len(m.relays))
	for _, channel := range m.relays {
		relays = append(relays, channel)
		channel.cancel()
	}
	m.mu.Unlock()
	for _, channel := range relays {
		select {
		case <-channel.done:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}
