// Package main provides a CLI for querying the BuildBuddy Twirp JSON API.
//
// BuildBuddy exposes its internal BuildBuddyService via a Twirp-style JSON API
// at app.buildbuddy.io/rpc/BuildBuddyService/<Method>. The gRPC endpoint
// (remote.buildbuddy.io) only serves Bazel-specific services (RBE, BES, cache).
package main

import (
	"bytes"
	"fmt"
	"io"
	"net/http"
	"os"

	invocationpb "github.com/buildbuddy-io/buildbuddy/proto/invocation"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

const defaultBaseURL = "https://app.buildbuddy.io"

type client struct {
	baseURL string
	apiKey  string
	http    *http.Client
	groupID string
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
	return &client{baseURL: base, apiKey: key, http: &http.Client{}}, nil
}

// call makes a Twirp JSON RPC call: POST /rpc/BuildBuddyService/<method>.
func (c *client) call(method string, req proto.Message, resp proto.Message) error {
	body, err := protojson.Marshal(req)
	if err != nil {
		return fmt.Errorf("marshal request: %w", err)
	}
	url := fmt.Sprintf("%s/rpc/BuildBuddyService/%s", c.baseURL, method)
	httpReq, err := http.NewRequest("POST", url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("x-buildbuddy-api-key", c.apiKey)

	httpResp, err := c.http.Do(httpReq)
	if err != nil {
		return fmt.Errorf("http request: %w", err)
	}
	defer httpResp.Body.Close()

	respBody, err := io.ReadAll(httpResp.Body)
	if err != nil {
		return fmt.Errorf("read response: %w", err)
	}
	if httpResp.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d: %s", httpResp.StatusCode, string(respBody))
	}
	if err := protojson.Unmarshal(respBody, resp); err != nil {
		return fmt.Errorf("unmarshal response: %w", err)
	}
	return nil
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
	resp := &invocationpb.SearchInvocationResponse{}
	if err := c.call("SearchInvocation", req, resp); err != nil {
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

// fetchURL does a GET with the API key header, returning raw bytes.
func (c *client) fetchURL(url string) ([]byte, error) {
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("x-buildbuddy-api-key", c.apiKey)

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(body))
	}
	return body, nil
}
