package supabase

import (
	"fmt"
	"log/slog"
	"os"
	"regexp"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/auth"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/mcp"
)

// Registration is the global MCP server registration for the Supabase server.
// Set during init() via mcp.NewRegistration.
// Binary: supabase.Registration (new in b71486df).
var Registration *mcp.ServerRegistration

// migrationNamePattern is the regexp for validating Supabase migration names.
// Binary: supabase.migrationNamePattern (new in b71486df).
// Value: "^[a-zA-Z0-9_]+$" (16 chars, observed in binary strings).
var migrationNamePattern = regexp.MustCompile(`^[a-zA-Z0-9_]+$`)

// init registers the Supabase MCP server with the global MCP registry.
//
// Binary: supabase.init (new in b71486df).
// Server name: "supabase" (8 chars).
// Description: "Supabase database management: migrations, SQL queries, and TypeScript type generation" (85 chars).
func init() {
	Registration = mcp.NewRegistration(
		"supabase",
		"Supabase database management: migrations, SQL queries, and TypeScript type generation",
		configureServer,
		shouldRegisterWrapper,
	)
}

// shouldRegisterWrapper is the init.func1 closure passed to NewRegistration.
// Binary: supabase.init.func1 (new in b71486df).
func shouldRegisterWrapper() bool {
	return shouldRegister(nil)
}

// shouldRegister determines whether the Supabase MCP server should be registered.
//
// Binary: supabase.shouldRegister (new in b71486df).
// Returns false if SUPABASE_MCP_DISABLED=true (21-char env var name).
// TODO(re): Also checks if environment type field at some offset equals "baku" (4 chars).
// The "baku" check references a struct field at offset 0xf0 in an unknown context struct.
func shouldRegister(ctx interface{}) bool {
	if os.Getenv("SUPABASE_MCP_DISABLED") == "true" {
		return false
	}
	// TODO(re): check if environment type == "baku"; returns true only for "baku" env type.
	// Binary evidence: CMP instruction comparing 4 bytes at offset 0xf0 against 0x756b6162 ("baku" LE).
	return false
}

// configureServer creates and configures a new SupabaseMCPServer instance.
//
// Binary: supabase.configureServer (new in b71486df).
// Validates PAT: "supabase PAT not configured in auth context" (43 chars).
// Validates ProjectRef: "supabase project ref not configured in auth context" (51 chars).
// Logs: "Configuring Supabase MCP server" (30 chars) with project_ref and anon_key.
// HTTP client timeout: 30s.
func configureServer(logger *slog.Logger, name string, envCfg interface{}, authCtxIface interface{}, sessionCfg interface{}) (mcp.MCPServer, error) {
	authCtx, ok := authCtxIface.(*auth.AuthContext)
	if !ok || authCtx == nil {
		return nil, fmt.Errorf("supabase PAT not configured in auth context")
	}

	if authCtx.GetSupabasePAT() == "" {
		return nil, fmt.Errorf("supabase PAT not configured in auth context")
	}
	if authCtx.GetSupabaseProjectRef() == "" {
		return nil, fmt.Errorf("supabase project ref not configured in auth context")
	}

	logger.Info("Configuring Supabase MCP server",
		"project_ref", authCtx.GetSupabaseProjectRef(),
		"anon_key", authCtx.GetSupabaseAnonKey(),
	)

	client := NewClient(
		authCtx.GetSupabasePAT(),
		authCtx.GetSupabaseProjectRef(),
		authCtx.GetSupabaseAnonKey(),
	)

	server := &SupabaseMCPServer{
		BaseServer: &mcp.BaseServer{},
		client:     client,
		authCtx:    authCtx,
		// TODO(re): workDir source not recovered; likely from envCfg or sessionCfg
	}
	_ = name

	return server, nil
}
