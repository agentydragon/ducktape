// Package main queries BuildBuddy using its generated gRPC API and HTTP endpoints.
package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	bbspb "github.com/buildbuddy-io/buildbuddy/proto/buildbuddy_service"
	invocationpb "github.com/buildbuddy-io/buildbuddy/proto/invocation"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

const defaultBaseURL = "https://app.buildbuddy.io"

type client struct {
	rpc     bbspb.BuildBuddyServiceClient
	conn    *grpc.ClientConn
	baseURL string
	apiKey  string
	http    *http.Client
	groupID string
	retries int
}

func newClient() (*client, error) {
	key := os.Getenv("BUILDBUDDY_API_KEY")
	if key == "" {
		return nil, fmt.Errorf("BUILDBUDDY_API_KEY environment variable is not set")
	}
	base := os.Getenv("BUILDBUDDY_URL")
	if base == "" {
		base = defaultBaseURL
	}
	conn, err := newRPCConnection(base, key, requestTimeout, 3)
	if err != nil {
		return nil, err
	}
	return &client{baseURL: base, apiKey: key, http: &http.Client{Timeout: requestTimeout}, retries: 3, conn: conn, rpc: bbspb.NewBuildBuddyServiceClient(conn)}, nil
}

func (c *client) close() {
	if c.conn != nil {
		_ = c.conn.Close()
	}
}

// call uses BuildBuddy's protolet HTTP adapter: POST /rpc/BuildBuddyService/<method>.
func (c *client) call(method string, req proto.Message, resp proto.Message) error {
	return c.callPath("/rpc/BuildBuddyService/"+method, req, resp)
}

func (c *client) callPath(path string, req proto.Message, resp proto.Message) error {
	body, err := protojson.Marshal(req)
	if err != nil {
		return fmt.Errorf("marshal request: %w", err)
	}
	url := c.baseURL + path
	httpReq, err := http.NewRequest("POST", url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("x-buildbuddy-api-key", c.apiKey)

	respBody, err := c.readResponse(httpReq, strings.Contains(path, "/Get") || strings.Contains(path, "/Search"))
	if err != nil {
		return err
	}
	if err := unmarshalJSON(respBody, resp); err != nil {
		return fmt.Errorf("unmarshal response: %w", err)
	}
	return nil
}

// unmarshalJSON parses a proto JSON message while discarding fields unknown to
// our pinned protos, so a newer BuildBuddy API or BES response doesn't
// hard-fail the parse. bbapi only reads named fields, so unknown ones are safe
// to drop; repinning keeps the schema current, and this guards against drift
// between repins (e.g. an invocation-link-type field added after the last pin).
func unmarshalJSON(b []byte, m proto.Message) error {
	return protojson.UnmarshalOptions{DiscardUnknown: true}.Unmarshal(b, m)
}

// resolveGroupID fetches the group_id by searching for a recent invocation
// and extracting it from the ACL. Many RPCs require request_context.group_id.
func (c *client) resolveGroupID(repo string) (string, error) {
	if c.groupID != "" {
		return c.groupID, nil
	}
	req := &invocationpb.SearchInvocationRequest{
		Query: &invocationpb.InvocationQuery{RepoUrl: repo},
		Count: 1,
	}
	resp, err := c.rpc.SearchInvocation(context.Background(), req)
	if err != nil {
		return "", fmt.Errorf("resolve group_id: %w", err)
	}
	for _, inv := range resp.GetInvocation() {
		if gid := inv.GetAcl().GetGroupId(); gid != "" {
			c.groupID = gid
			return gid, nil
		}
	}
	return "", fmt.Errorf("no invocations found for repo %s; cannot determine group_id", repo)
}

type httpError struct {
	status int
	body   string
}

func (e *httpError) Error() string { return fmt.Sprintf("HTTP %d: %s", e.status, e.body) }

// readResponse retries idempotent reads on transient failures. Mutation RPCs
// such as CancelExecutions are sent once so retries cannot duplicate effects.
func (c *client) readResponse(req *http.Request, retry bool) ([]byte, error) {
	var body []byte
	err := c.consumeResponse(req, retry, func(r io.Reader) error {
		var err error
		body, err = io.ReadAll(r)
		return err
	})
	return body, err
}

// consumeResponse may invoke consume again after a partial response; consumers
// must reset any output from the previous attempt.
func (c *client) consumeResponse(req *http.Request, retry bool, consume func(io.Reader) error) error {
	attempts := 1
	if retry {
		attempts += c.retries
	}
	var last error
	for attempt := 0; attempt < attempts; attempt++ {
		next := req.Clone(req.Context())
		if req.GetBody != nil {
			body, err := req.GetBody()
			if err != nil {
				return err
			}
			next.Body = body
		}
		err := c.consumeResponseOnce(next, consume)
		if err == nil {
			return nil
		}
		last = err
		// Retrying cannot fix local destination failures.
		var local *os.PathError
		if errors.As(err, &local) {
			return err
		}
		var status *httpError
		if errors.As(err, &status) && status.status != 408 && status.status != 425 && status.status != 429 && status.status < 500 {
			return err
		}
		if attempt+1 < attempts {
			time.Sleep(time.Second * time.Duration(1<<attempt))
		}
	}
	return last
}

func (c *client) consumeResponseOnce(req *http.Request, consume func(io.Reader) error) error {
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, err := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
		if err != nil {
			return fmt.Errorf("read response: %w", err)
		}
		return &httpError{status: resp.StatusCode, body: string(body)}
	}
	return consume(resp.Body)
}

// fetchURL does a GET with the API key header, returning raw bytes.
func (c *client) fetchURL(url string) ([]byte, error) {
	var body []byte
	err := c.consumeURL(url, func(r io.Reader) error {
		var err error
		body, err = io.ReadAll(r)
		return err
	})
	return body, err
}

func (c *client) consumeURL(url string, consume func(io.Reader) error) error {
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("x-buildbuddy-api-key", c.apiKey)
	return c.consumeResponse(req, true, consume)
}

// bytestreamDownloadURL includes invocation_id because BuildBuddy's legacy
// artifact fallback uses it to authorize and locate blobs that are no longer
// present in the CAS cache.
func (c *client) bytestreamDownloadURL(invocationID, uri string) string {
	params := url.Values{}
	params.Set("invocation_id", invocationID)
	params.Set("bytestream_url", uri)
	return fmt.Sprintf("%s/file/download?%s", c.baseURL, params.Encode())
}
