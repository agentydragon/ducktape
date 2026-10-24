// Package historyclient adapts the generated History Service client to Agentplane app readers and
// watch workers. Each instance owns its channel so one worker can redial without disturbing reads.
package historyclient

import (
	"context"
	"errors"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"google.golang.org/grpc"
)

// Config pins this client's service address and rotating projected bearer token.
type Config struct {
	Target    string
	TokenFile string
	Timeout   time.Duration
}

// Client is one independently redialable generated History Service connection.
type Client struct {
	config Config
	mu     sync.RWMutex
	conn   *grpc.ClientConn
}

// New constructs a client without waiting for the remote service to be reachable. gRPC connects
// lazily, and the bearer interceptor rereads the projected token for every RPC.
func New(config Config) (*Client, error) {
	if config.Target == "" || config.TokenFile == "" || config.Timeout <= 0 {
		return nil, errors.New("History Service target, token file, and positive timeout are required")
	}
	client := &Client{config: config}
	if err := client.dialLocked(); err != nil {
		return nil, err
	}
	return client, nil
}

func (c *Client) dialLocked() error {
	conn, err := bearer.Dial(c.config.Target, c.config.TokenFile, c.config.Timeout)
	if err != nil {
		return err
	}
	c.conn = conn
	return nil
}

func (c *Client) connection() (*grpc.ClientConn, error) {
	c.mu.RLock()
	conn := c.conn
	c.mu.RUnlock()
	if conn != nil {
		return conn, nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.conn == nil {
		if err := c.dialLocked(); err != nil {
			return nil, err
		}
	}
	return c.conn, nil
}

// Redial replaces only this client's channel. History workers use dedicated Client values, so
// repeated watch failures cannot interrupt another worker's reads.
func (c *Client) Redial() {
	c.mu.Lock()
	old := c.conn
	c.conn = nil
	err := c.dialLocked()
	c.mu.Unlock()
	if old != nil {
		_ = old.Close()
	}
	if err != nil {
		// Leave conn nil: the next RPC retries construction and reports the failure through its
		// ordinary service error path rather than terminating the worker at redial time.
		return
	}
}

func (c *Client) grpcClient() (historypb.HistoryServiceClient, error) {
	conn, err := c.connection()
	if err != nil {
		return nil, err
	}
	return historypb.NewHistoryServiceClient(conn), nil
}

func (c *Client) ReadSessionEvents(ctx context.Context, request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
	client, err := c.grpcClient()
	if err != nil {
		return nil, err
	}
	return client.ReadSessionEvents(ctx, request)
}

func (c *Client) ReadSessionObservations(ctx context.Context, request *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error) {
	client, err := c.grpcClient()
	if err != nil {
		return nil, err
	}
	return client.ReadSessionObservations(ctx, request)
}

func (c *Client) GetSessionSummary(ctx context.Context, request *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	client, err := c.grpcClient()
	if err != nil {
		return nil, err
	}
	return client.GetSessionSummary(ctx, request)
}

func (c *Client) BatchGetSessionSummaries(ctx context.Context, request *historypb.BatchGetSessionSummariesRequest) (*historypb.BatchGetSessionSummariesResponse, error) {
	client, err := c.grpcClient()
	if err != nil {
		return nil, err
	}
	return client.BatchGetSessionSummaries(ctx, request)
}

func (c *Client) ReadThreadEntities(ctx context.Context, request *historypb.ReadThreadEntitiesRequest) (*historypb.ReadThreadEntitiesResponse, error) {
	client, err := c.grpcClient()
	if err != nil {
		return nil, err
	}
	return client.ReadThreadEntities(ctx, request)
}

func (c *Client) ReadEntityEvidence(ctx context.Context, request *historypb.ReadEntityEvidenceRequest) (*historypb.ReadEntityEvidenceResponse, error) {
	client, err := c.grpcClient()
	if err != nil {
		return nil, err
	}
	return client.ReadEntityEvidence(ctx, request)
}

func (c *Client) ReadNativeFrames(ctx context.Context, request *historypb.ReadNativeFramesRequest) (*historypb.ReadNativeFramesResponse, error) {
	client, err := c.grpcClient()
	if err != nil {
		return nil, err
	}
	return client.ReadNativeFrames(ctx, request)
}

func (c *Client) FindSessions(ctx context.Context, request *historypb.FindSessionsRequest) (*historypb.FindSessionsResponse, error) {
	client, err := c.grpcClient()
	if err != nil {
		return nil, err
	}
	return client.FindSessions(ctx, request)
}

func (c *Client) WatchSessionSummaries(ctx context.Context, request *historypb.WatchSessionSummariesRequest) (historypb.HistoryService_WatchSessionSummariesClient, error) {
	client, err := c.grpcClient()
	if err != nil {
		return nil, err
	}
	return client.WatchSessionSummaries(ctx, request)
}

// Close releases this worker's channel when the process shuts down.
func (c *Client) Close() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.conn == nil {
		return nil
	}
	err := c.conn.Close()
	c.conn = nil
	return err
}

var _ interface {
	ReadSessionEvents(context.Context, *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error)
	ReadSessionObservations(context.Context, *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error)
	GetSessionSummary(context.Context, *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error)
	ReadThreadEntities(context.Context, *historypb.ReadThreadEntitiesRequest) (*historypb.ReadThreadEntitiesResponse, error)
	ReadEntityEvidence(context.Context, *historypb.ReadEntityEvidenceRequest) (*historypb.ReadEntityEvidenceResponse, error)
	ReadNativeFrames(context.Context, *historypb.ReadNativeFramesRequest) (*historypb.ReadNativeFramesResponse, error)
	WatchSessionSummaries(context.Context, *historypb.WatchSessionSummariesRequest) (historypb.HistoryService_WatchSessionSummariesClient, error)
	Redial()
} = (*Client)(nil)
