// Package supabase implements the Supabase MCP server for database management.
// It provides tools for running SQL queries, managing migrations, and generating
// TypeScript type definitions.
//
// Reconstructed from binary b71486df (Go 1.25.6).
// Source path: .../environment-manager/internal/mcp/servers/supabase/
//
// New symbols in b71486df (vs 6b49f1ca):
//   - supabase.(*Client).ApplyMigration
//   - supabase.(*Client).GenerateTypes
//   - supabase.(*Client).ListMigrations
//   - supabase.(*Client).RunQuery
//   - supabase.(*Client).doRequest
//   - supabase.(*Client).doRequest.deferwrap1
package supabase

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// Client is an HTTP client for the Supabase Management API.
//
// Binary: allocated in configureServer, passed to SupabaseMCPServer.
// HTTP client timeout: 0x6fc23ac00 ns = 30,000,000,000 ns = 30s.
// Base URL: "https://api.supabase.com/v1" (27 chars).
// Auth: Authorization: Bearer <PAT> header.
//
// TODO(re): struct layout offsets not fully confirmed; field order inferred
// from configureServer assembly and doRequest field accesses.
type Client struct {
	httpClient *http.Client // timeout 30s
	pat        string       // Personal Access Token (from AuthContext.GetSupabasePAT)
	projectRef string       // Supabase project ref (from AuthContext.GetSupabaseProjectRef)
	anonKey    string       // Supabase anon key (from AuthContext.GetSupabaseAnonKey)
}

// newClient creates a new Supabase API client.
// Binary: called from configureServer.
func newClient(pat, projectRef, anonKey string) *Client {
	return &Client{
		httpClient: &http.Client{Timeout: 30 * time.Second},
		pat:        pat,
		projectRef: projectRef,
		anonKey:    anonKey,
	}
}

// doRequest makes an authenticated HTTP request to the Supabase Management API.
//
// Binary address: 0xb139e0 (approximate, new in b71486df).
// Sets Authorization: Bearer <PAT> header.
// URL format: "https://api.supabase.com/v1" + path.
// Sets Content-Type: application/json when body is non-empty.
func (c *Client) doRequest(ctx context.Context, method, path, body string) (*http.Response, error) {
	url := "https://api.supabase.com/v1" + path

	var bodyReader *strings.Reader
	if body != "" {
		bodyReader = strings.NewReader(body)
	}

	var req *http.Request
	var err error
	if bodyReader != nil {
		req, err = http.NewRequestWithContext(ctx, method, url, bodyReader)
	} else {
		req, err = http.NewRequestWithContext(ctx, method, url, nil)
	}
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.Header.Set("Authorization", "Bearer "+c.pat)
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("supabase request failed: %w", err)
	}
	return resp, nil
}

// ApplyMigration applies a SQL migration to the Supabase project database.
//
// Binary: supabase.(*Client).ApplyMigration (new in b71486df).
// Validates migration name against migrationNamePattern.
// POST /projects/{ref}/database/migrations.
// TODO(re): request/response format not fully recovered; marshal error: "marshal migration: %w"
func (c *Client) ApplyMigration(ctx context.Context, name, sql string) error {
	// TODO(re): validate name against migrationNamePattern before proceeding
	// TODO(re): marshal migration name+sql to JSON body
	// TODO(re): POST /projects/{ref}/database/migrations
	// TODO(re): check response status; return error on failure
	return fmt.Errorf("TODO(re): ApplyMigration not reconstructed")
}

// RunQuery executes a SQL query against the Supabase project database.
//
// Binary: supabase.(*Client).RunQuery (new in b71486df).
// Validates that sql is not empty: "sql must not be empty" (20 chars).
// POST /projects/{ref}/database/query.
// TODO(re): response type not recovered; QueryErrorResponse parsed on error.
func (c *Client) RunQuery(ctx context.Context, query string) (interface{}, error) {
	if query == "" {
		return nil, fmt.Errorf("sql must not be empty")
	}
	// TODO(re): POST /projects/{ref}/database/query with JSON body
	// TODO(re): parse QueryErrorResponse on non-2xx response
	return nil, fmt.Errorf("TODO(re): RunQuery not reconstructed")
}

// ListMigrations lists the applied migrations for the Supabase project.
//
// Binary: supabase.(*Client).ListMigrations (new in b71486df).
// GET /projects/{ref}/database/migrations.
// Returns []MigrationResponse.
// TODO(re): response parsing not recovered.
func (c *Client) ListMigrations(ctx context.Context) ([]MigrationResponse, error) {
	// TODO(re): GET /projects/{ref}/database/migrations
	// TODO(re): parse JSON response into []MigrationResponse
	return nil, fmt.Errorf("TODO(re): ListMigrations not reconstructed")
}

// GenerateTypes generates TypeScript type definitions for the Supabase project.
//
// Binary: supabase.(*Client).GenerateTypes (new in b71486df).
// Writes output to a file in the project directory.
// Error strings: "Failed to write types file" (26 chars), "Type generation failed: %v" (26 chars).
// TODO(re): exact API endpoint and file write logic not recovered.
func (c *Client) GenerateTypes(ctx context.Context, projectDir string) error {
	// TODO(re): GET Supabase types generation endpoint
	// TODO(re): write response to file; error: "Failed to write types file"
	// TODO(re): on API failure: "Type generation failed: %v"
	return fmt.Errorf("TODO(re): GenerateTypes not reconstructed")
}
