package gateway

import (
	"sync"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const runnerConnectionUnavailable = "runner connection is not available on this Gateway replica"

var errSupersededConnection = status.Error(codes.Aborted, "superseded by a newer connection")

type runnerConnection struct {
	identity    *sandboxreader.Identity
	podUID      string
	bootID      string
	runner      environmentruntimepb.EnvironmentRuntimeClient
	direct      *fencedRunnerConn
	closeDirect func()
	ended       chan struct{}
	endMu       sync.Mutex
	endErr      error
}

func (c *runnerConnection) end(err error) {
	c.endMu.Lock()
	select {
	case <-c.ended:
		c.endMu.Unlock()
		return
	default:
		c.endErr = err
		close(c.ended)
	}
	c.endMu.Unlock()
	if c.closeDirect != nil {
		c.closeDirect()
	}
}

func (c *runnerConnection) error() error {
	c.endMu.Lock()
	defer c.endMu.Unlock()
	if c.endErr != nil {
		return c.endErr
	}
	return status.Error(codes.Unavailable, "runner observation ended")
}

// connections is this replica's table of authenticated Runtime observations.
type connections struct {
	mu    sync.RWMutex
	local map[string]*runnerConnection
}

func newConnections() *connections {
	return &connections{local: make(map[string]*runnerConnection)}
}

func (c *connections) accept(connection *runnerConnection) {
	c.mu.Lock()
	previous := c.local[connection.identity.Uid]
	c.local[connection.identity.Uid] = connection
	c.mu.Unlock()
	if previous != nil {
		previous.end(errSupersededConnection)
	}
}

func (c *connections) remove(connection *runnerConnection, err error) {
	c.mu.Lock()
	if c.local[connection.identity.Uid] == connection {
		delete(c.local, connection.identity.Uid)
	}
	c.mu.Unlock()
	connection.end(err)
}

func (c *connections) holder(sandboxUID string) (*runnerConnection, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	connection := c.local[sandboxUID]
	if connection == nil {
		return nil, status.Error(codes.Unavailable, runnerConnectionUnavailable)
	}
	select {
	case <-connection.ended:
		if err := connection.error(); err != nil {
			return nil, err
		}
		return nil, status.Error(codes.Unavailable, "runner observation ended")
	default:
		return connection, nil
	}
}
