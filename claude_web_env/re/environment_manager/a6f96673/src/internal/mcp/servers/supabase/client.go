// Package supabase implements the Supabase MCP server for database management.
// It provides tools for running SQL queries, managing migrations, and generating
// TypeScript type definitions.
//
// Reconstructed from binary b71486df (Go 1.25.6).
// Source path: .../environment-manager/internal/mcp/servers/supabase/
//
// New symbols in b71486df (vs 6b49f1ca):
//   - supabase.NewClient (exported constructor)
//   - supabase.(*Client).ApplyMigration
//   - supabase.(*Client).GenerateTypes
//   - supabase.(*Client).ListMigrations
//   - supabase.(*Client).RunQuery
//   - supabase.(*Client).doRequest
//   - supabase.(*Client).doRequest.deferwrap1
package supabase

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
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

// NewClient creates a new Supabase API client.
//
// Binary: supabase.NewClient (exported symbol in b71486df; confirmed by nm and go tool objdump).
// Called from configureServer in registration.go.
func NewClient(pat, projectRef, anonKey string) *Client {
	return &Client{
		httpClient: &http.Client{Timeout: 30 * time.Second},
		pat:        pat,
		projectRef: projectRef,
		anonKey:    anonKey,
	}
}

// doRequest makes an authenticated HTTP request to the Supabase Management API.
//
// Binary address: supabase.(*Client).doRequest (new in b71486df).
// Sets Authorization: Bearer <PAT> header.
// URL format: "https://api.supabase.com/v1" + path.
// Sets Content-Type: application/json when body is non-empty.
// Reads and returns response body as string; returns error on status >= 400.
// On non-2xx: calls json.Unmarshal into QueryErrorResponse before returning error.
//
// Error strings (binary evidence):
//   - "create request: %w" (18 chars)
//   - "http request: %w" (16 chars)
//   - "read response: %w" (17 chars)
//   - "API error %d: %s" (16 chars)
func (c *Client) doRequest(ctx context.Context, method, path, body string) (string, error) {
	url := "https://api.supabase.com/v1" + path

	var bodyReader io.Reader
	if body != "" {
		bodyReader = strings.NewReader(body)
	}

	req, err := http.NewRequestWithContext(ctx, method, url, bodyReader)
	if err != nil {
		return "", fmt.Errorf("create request: %w", err)
	}

	req.Header.Set("Authorization", "Bearer "+c.pat)
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode >= 400 {
		// Binary calls json.Unmarshal into QueryErrorResponse before returning error.
		// TODO(re): QueryErrorResponse fields not recovered; structured error message
		// format unknown. Unmarshal performed but result not used in error formatting.
		var errResp QueryErrorResponse
		_ = json.Unmarshal(respBody, &errResp)
		return "", fmt.Errorf("API error %d: %s", resp.StatusCode, respBody)
	}

	return string(respBody), nil
}

// ApplyMigration applies a SQL migration to the Supabase project database.
//
// Binary: supabase.(*Client).ApplyMigration (new in b71486df).
// POST /projects/{ref}/database/migrations with JSON body {"name": name, "query": sql}.
//
// Error strings (binary evidence):
//   - "marshal migration: %w" (21 chars)
//   - "apply migration: %w" (19 chars)
func (c *Client) ApplyMigration(ctx context.Context, name, sql string) error {
	body := map[string]string{
		"name":  name,
		"query": sql,
	}
	bodyJSON, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("marshal migration: %w", err)
	}

	path := fmt.Sprintf("/projects/%s/database/migrations", c.projectRef)
	_, err = c.doRequest(ctx, http.MethodPost, path, string(bodyJSON))
	if err != nil {
		return fmt.Errorf("apply migration: %w", err)
	}
	return nil
}

// RunQuery executes a SQL query against the Supabase project database.
//
// Binary: supabase.(*Client).RunQuery (new in b71486df).
// POST /projects/{ref}/database/query with JSON body {"query": query}.
// Returns raw response body string.
//
// Error strings (binary evidence):
//   - "marshal query: %w" (17 chars)
//   - "run query: %w" (13 chars)
func (c *Client) RunQuery(ctx context.Context, query string) (string, error) {
	body := map[string]string{
		"query": query,
	}
	bodyJSON, err := json.Marshal(body)
	if err != nil {
		return "", fmt.Errorf("marshal query: %w", err)
	}

	path := fmt.Sprintf("/projects/%s/database/query", c.projectRef)
	result, err := c.doRequest(ctx, http.MethodPost, path, string(bodyJSON))
	if err != nil {
		return "", fmt.Errorf("run query: %w", err)
	}
	return result, nil
}

// ListMigrations lists the applied migrations for the Supabase project.
//
// Binary: supabase.(*Client).ListMigrations (new in b71486df).
// GET /projects/{ref}/database/migrations.
// Returns []MigrationResponse.
//
// Error strings (binary evidence):
//   - "list migrations: %w" (19 chars)
//   - "parse migrations: %w" (20 chars)
func (c *Client) ListMigrations(ctx context.Context) ([]MigrationResponse, error) {
	path := fmt.Sprintf("/projects/%s/database/migrations", c.projectRef)
	body, err := c.doRequest(ctx, http.MethodGet, path, "")
	if err != nil {
		return nil, fmt.Errorf("list migrations: %w", err)
	}

	var migrations []MigrationResponse
	if err := json.Unmarshal([]byte(body), &migrations); err != nil {
		return nil, fmt.Errorf("parse migrations: %w", err)
	}
	return migrations, nil
}

// GenerateTypes generates TypeScript type definitions for the Supabase project schema.
//
// Binary: supabase.(*Client).GenerateTypes (new in b71486df).
// GET /projects/{ref}/types/typescript.
// Returns the TypeScript types string.
//
// Error strings (binary evidence):
//   - "generate types: %w" (18 chars)
//   - "parse types response: %w" (23 chars)
func (c *Client) GenerateTypes(ctx context.Context) (string, error) {
	path := fmt.Sprintf("/projects/%s/types/typescript", c.projectRef)
	body, err := c.doRequest(ctx, http.MethodGet, path, "")
	if err != nil {
		return "", fmt.Errorf("generate types: %w", err)
	}

	var resp TypesResponse
	if err := json.Unmarshal([]byte(body), &resp); err != nil {
		return "", fmt.Errorf("parse types response: %w", err)
	}
	return resp.Types, nil
}
