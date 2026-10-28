package gateway

import (
	"sync"

	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const connectionHeldByAnotherReplica = "another replica holds this Sandbox's runner connection"

var errSupersededConnection = status.Error(codes.Aborted, "superseded by a newer connection")

type runnerConnection struct {
	identity sandboxreader.Identity
	podUID   string
	epoch    uint64
	bootID   string
	tunnel   *tunnel
	runner   runnerservicepb.RunnerClient
	ended    chan struct{}
	endMu    sync.Mutex
	endErr   error
}

func newRunnerConnection(identity sandboxreader.Identity, podUID, bootID string, epoch uint64, send sendServiceFrame) *runnerConnection {
	tunnel := newTunnel(send)
	return &runnerConnection{
		identity: identity, podUID: podUID, epoch: epoch, bootID: bootID,
		tunnel: tunnel, runner: runnerservicepb.NewRunnerClient(&tunnelClientConn{tunnel: tunnel}), ended: make(chan struct{}),
	}
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
	c.tunnel.close(err)
}

func (c *runnerConnection) error() error {
	c.endMu.Lock()
	defer c.endMu.Unlock()
	if c.endErr != nil {
		return c.endErr
	}
	return status.Error(codes.Unavailable, "runner control stream ended")
}

// connections is the local replica's stream table and watch-derived view of Lease epochs.
type connections struct {
	mu          sync.RWMutex
	local       map[string]*runnerConnection
	leaseEpochs map[string]uint64
}

func newConnections() *connections {
	return &connections{local: make(map[string]*runnerConnection), leaseEpochs: make(map[string]uint64)}
}

func (c *connections) accept(connection *runnerConnection) {
	c.mu.Lock()
	previous := c.local[connection.identity.UID]
	c.local[connection.identity.UID] = connection
	c.mu.Unlock()
	if previous != nil {
		previous.end(errSupersededConnection)
	}
}

func (c *connections) remove(connection *runnerConnection, err error) {
	c.mu.Lock()
	if c.local[connection.identity.UID] == connection {
		delete(c.local, connection.identity.UID)
	}
	c.mu.Unlock()
	connection.end(err)
}

func (c *connections) holder(sandboxUID string) (*runnerConnection, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	connection := c.local[sandboxUID]
	if connection == nil {
		return nil, status.Error(codes.Unavailable, connectionHeldByAnotherReplica)
	}
	if c.leaseEpochs[sandboxUID] > connection.epoch {
		return nil, status.Error(codes.Unavailable, connectionHeldByAnotherReplica)
	}
	select {
	case <-connection.ended:
		if err := connection.error(); err != nil {
			return nil, err
		}
		return nil, status.Error(codes.Unavailable, "runner control stream ended")
	default:
		return connection, nil
	}
}

func (c *connections) forgetLease(sandboxUID string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.leaseEpochs, sandboxUID)
}

func (c *connections) replaceLease(previousUID, currentUID string, epoch uint64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if previousUID != "" && previousUID != currentUID {
		delete(c.leaseEpochs, previousUID)
	}
	if currentUID != "" {
		c.leaseEpochs[currentUID] = epoch
	}
}
