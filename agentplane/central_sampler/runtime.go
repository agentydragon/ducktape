package centralsampler

import (
	"context"
	"sync"
	"time"

	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/settings"
)

type Runtime struct {
	store        *Store
	settings     samplersettings.Settings
	claimLease   func(context.Context, string, time.Duration) (Owner, error)
	releaseLease func(context.Context, Owner) error
	newExecutor  func(context.Context, *Store, Owner, *Session, samplersettings.Settings) (*Executor, func() error, error)

	mu               sync.Mutex
	runCtx           context.Context
	cancel           context.CancelFunc
	started          bool
	stopping         bool
	draining         chan struct{}
	shutdownDeadline time.Time
	tasks            map[string]*residentSession
	executing        chan struct{}
	changed          chan struct{}
	drivers          sync.WaitGroup
	codeHost         *CodeModeHost
}

type residentSession struct {
	// owner and busy are guarded by Runtime.mu; wake carries no authoritative work.
	wake  chan struct{}
	owner *Owner
	busy  bool
}

func (s *residentSession) signal() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

// NewRuntime takes the shared Code Mode host it attaches logical sessions to and shuts down.
func NewRuntime(store *Store, settings samplersettings.Settings, codeHost *CodeModeHost) *Runtime {
	return &Runtime{
		store: store, settings: settings, codeHost: codeHost, claimLease: store.Claim, releaseLease: store.Release, newExecutor: NewExecutor,
		tasks: map[string]*residentSession{}, executing: make(chan struct{}, settings.MaxLiveSessions),
		changed: make(chan struct{}, 1), draining: make(chan struct{}),
	}
}
