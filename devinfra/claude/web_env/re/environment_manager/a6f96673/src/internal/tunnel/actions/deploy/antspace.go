// Reconstructed from binary: environment-manager (Build ID a6f96673)
// Source: internal/tunnel/actions/deploy/antspace.go
// Module: github.com/anthropics/anthropic/api-go/environment-manager
//
// Antspace deployment client - deploys Edge Functions to Anthropic's control plane.
// Antspace deployment client - deploys Edge Functions to Anthropic's
// Antspace control plane (alternative to Vercel deployments).

package deploy

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/tunnel/actions"
)

// AntspaceClient handles deployment to Anthropic's Antspace control plane.
//
// Binary type: *deploy.AntspaceClient
// Source: antspace.go
//
// Struct layout:
//
//	offset 0x00: ControlPlaneURL string (ptr + len)
//	offset 0x10: AuthToken string (ptr + len)
//	offset 0x20: Logger *slog.Logger
type AntspaceClient struct {
	ControlPlaneURL string       // offset 0x00
	AuthToken       string       // offset 0x10
	Logger          *slog.Logger // offset 0x20
}

// AntspaceDeployResult holds the result of an Antspace deployment.
//
// Binary type: *deploy.AntspaceDeployResult
type AntspaceDeployResult struct {
	URL    string `json:"url"`
	Status string `json:"status"`
}

// deployRequest is the internal request body for Antspace deployments.
//
// Binary type: deploy.deployRequest
type deployRequest struct {
	ProjectDir string `json:"project_dir"`
	Slug       string `json:"slug"`
}

// Deploy performs an Antspace deployment by:
// 1. Creating a multipart form with the tarball
// 2. Sending to the control plane URL
// 3. Reading and parsing the deploy response
//
// Binary: 0xb9dc00 - (*AntspaceClient).Deploy
// Source: antspace.go:126
//
// Flow:
//  1. Create bytes.Buffer and multipart.Writer (antspace.go:132-133)
//  2. Create form file for tarball
//  3. Write tarball data to form
//  4. Close multipart writer
//  5. Build HTTP POST request to control plane URL
//  6. Set Authorization header with auth token
//  7. Set Content-Type to multipart form
//  8. Execute request
//  9. Read and parse response via readDeployResponse
func (c *AntspaceClient) Deploy(
	ctx context.Context,
	slug string,
	tarball []byte,
	reporter actions.ProgressReporter,
) (*AntspaceDeployResult, error) {
	// antspace.go:132 - Create buffer and multipart writer
	var buf bytes.Buffer
	writer := multipart.NewWriter(&buf)

	// Create form file for the tarball
	part, err := writer.CreateFormFile("file", "deploy.tar.gz")
	if err != nil {
		return nil, fmt.Errorf("create form file %q: %w", "file", err)
	}

	// Write tarball data
	if _, err := part.Write(tarball); err != nil {
		return nil, fmt.Errorf("failed to write tarball: %w", err)
	}

	// Close the multipart writer
	if err := writer.Close(); err != nil {
		return nil, fmt.Errorf("failed to close tar writer: %w", err)
	}

	// Build the request URL
	url := fmt.Sprintf("%s/projects/%s/functions/deploy?slug=%s", c.ControlPlaneURL, slug, slug)

	// Create HTTP request
	req, err := http.NewRequestWithContext(ctx, "POST", url, &buf)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %v", err)
	}

	// Set headers
	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.Header.Set("Authorization", "Bearer "+c.AuthToken)

	// Execute request
	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("deploy request failed: %w", err)
	}
	defer resp.Body.Close()

	// Read and parse response
	return c.readDeployResponse(resp)
}

// readDeployResponse reads and parses the HTTP response from an Antspace
// deployment request.
//
// Binary: 0xb9e180 (approx) - (*AntspaceClient).readDeployResponse
// Source: antspace.go
func (c *AntspaceClient) readDeployResponse(resp *http.Response) (*AntspaceDeployResult, error) {
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("deployment failed (status %d): %s", resp.StatusCode, string(body))
	}

	var result AntspaceDeployResult
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("failed to parse deployment response: %w", err)
	}

	return &result, nil
}
