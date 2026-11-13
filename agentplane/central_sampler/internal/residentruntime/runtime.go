package residentruntime

import (
	"context"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/executor"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"

	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
)

type Runtime struct {
	store        *sessionstore.Store
	settings     samplersettings.Settings
	claimLease   func(context.Context, string, time.Duration) (sessionstore.Owner, error)
	releaseLease func(context.Context, sessionstore.Owner) error
	newExecutor  func(context.Context, *sessionstore.Store, sessionstore.Owner, *sessionstore.Session, samplersettings.Settings) (*executor.Executor, func() error, error)

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
	// These fields are guarded by Runtime.mu; wake carries no authoritative work.
	wake            chan struct{}
	owner           *sessionstore.Owner
	busy            bool
	waitingProvider bool
}

func (s *residentSession) signal() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

// New takes the shared Code Mode host it attaches logical sessions to and shuts down.
func New(store *sessionstore.Store, settings samplersettings.Settings, codeHost *CodeModeHost) *Runtime {
	return &Runtime{
		store: store, settings: settings, codeHost: codeHost, claimLease: store.Claim, releaseLease: store.Release, newExecutor: executor.New,
		tasks: map[string]*residentSession{}, executing: make(chan struct{}, settings.MaxLiveSessions),
		changed: make(chan struct{}, 1), draining: make(chan struct{}),
	}
}
