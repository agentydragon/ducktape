package runtimeproxy

import (
	"sync"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const runtimeConnectionUnavailable = "Environment Runtime connection is not available on this replica"

var errSupersededConnection = status.Error(codes.Aborted, "superseded by a newer connection")

type runtimeConnection struct {
	identity    *sandboxreader.Identity
	podUID      string
	bootID      string
	runtime     environmentruntimepb.EnvironmentRuntimeClient
	harnessHost harnesshostpb.HarnessHostClient
	direct      *fencedRuntimeConn
	closeDirect func()
	ended       chan struct{}
	endMu       sync.Mutex
	endErr      error
}

func (c *runtimeConnection) end(err error) {
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

func (c *runtimeConnection) error() error {
	c.endMu.Lock()
	defer c.endMu.Unlock()
	if c.endErr != nil {
		return c.endErr
	}
	return status.Error(codes.Unavailable, "Environment Runtime observation ended")
}

// connections is this replica's table of authenticated Runtime observations.
type connections struct {
	mu    sync.RWMutex
	local map[string]*runtimeConnection
}

func newConnections() *connections {
	return &connections{local: make(map[string]*runtimeConnection)}
}

func (c *connections) accept(connection *runtimeConnection) {
	c.mu.Lock()
	previous := c.local[connection.identity.Uid]
	c.local[connection.identity.Uid] = connection
	c.mu.Unlock()
	if previous != nil {
		previous.end(errSupersededConnection)
	}
}

func (c *connections) remove(connection *runtimeConnection, err error) {
	c.mu.Lock()
	if c.local[connection.identity.Uid] == connection {
		delete(c.local, connection.identity.Uid)
	}
	c.mu.Unlock()
	connection.end(err)
}

func (c *connections) holder(sandboxUID string) (*runtimeConnection, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	connection := c.local[sandboxUID]
	if connection == nil {
		return nil, status.Error(codes.Unavailable, runtimeConnectionUnavailable)
	}
	select {
	case <-connection.ended:
		return nil, connection.error()
	default:
		return connection, nil
	}
}
