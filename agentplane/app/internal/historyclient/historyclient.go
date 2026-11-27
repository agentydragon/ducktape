// Package historyclient adapts the generated History Service client to App readers.
package historyclient

import (
	"context"
	"errors"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/proto/journalstatspb"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"google.golang.org/grpc"
)

// Config pins this client's service address and caller authentication settings.
type Config struct {
	TLS       *peertls.ClientTLS
	Target    string
	TokenFile string
	Timeout   time.Duration
}

// Client owns the generated History Service connection.
type Client struct {
	conn *grpc.ClientConn
}

// New constructs a client without waiting for the remote service to be reachable. gRPC connects
// lazily, and bearer mode rereads its projected token for every RPC.
func New(config Config) (*Client, error) {
	if config.Target == "" || config.Timeout <= 0 {
		return nil, errors.New("History Service target and positive timeout are required")
	}
	if err := config.TLS.ValidateCaller(config.TokenFile, peertls.AppURI); err != nil {
		return nil, errors.New("History Service caller authentication is invalid")
	}
	conn, err := bearer.Dial(config.Target, config.TokenFile, config.Timeout, config.TLS)
	if err != nil {
		return nil, err
	}
	return &Client{conn: conn}, nil
}

func (c *Client) GetSessionSummary(ctx context.Context, request *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	return historypb.NewHistoryServiceClient(c.conn).GetSessionSummary(ctx, request)
}

func (c *Client) JournalStats() pb.JournalStatsServiceClient {
	return pb.NewJournalStatsServiceClient(c.conn)
}

// Close releases the channel when the process shuts down.
func (c *Client) Close() error { return c.conn.Close() }
