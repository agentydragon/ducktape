package residentruntime

import (
	"context"
	"errors"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/executor"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
)

type liveHost struct {
	code           *CodeMode
	executor       *executor.Executor
	closeTransport func() error
	closeOnce      sync.Once
	closeErr       error
}

func (h *liveHost) Close(ctx context.Context) error {
	var errs []error
	if h.code != nil {
		errs = append(errs, h.code.Detach(ctx))
	}
	errs = append(errs, h.CloseTransport())
	return errors.Join(errs...)
}

func (h *liveHost) CloseTransport() error {
	h.closeOnce.Do(func() {
		if h.closeTransport != nil {
			h.closeErr = h.closeTransport()
		}
	})
	return h.closeErr
}

func (r *Runtime) connectHost(ctx context.Context, sessionID string, owner sessionstore.Owner) (*liveHost, error) {
	session, err := r.store.GetSession(ctx, sessionID, "")
	if err != nil {
		return nil, err
	}
	executor, closeTransport, err := r.newExecutor(ctx, r.store, owner, session, r.settings)
	if err != nil {
		return nil, err
	}
	code, err := r.codeHost.OpenSession(ctx, owner.ConversationID, owner, executor)
	if err != nil {
		_ = closeTransport()
		return nil, err
	}
	return &liveHost{code: code, executor: executor, closeTransport: closeTransport}, nil
}

func liveDone(live *liveHost) <-chan struct{} {
	if live == nil || live.code == nil {
		return nil
	}
	return live.code.Done()
}
