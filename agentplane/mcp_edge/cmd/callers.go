package main

import (
	"context"
	"log/slog"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/internal/connections"
	"github.com/agentydragon/ducktape/agentplane/policy/identitycache"
)

// identitySource is what the edge needs of an Identity view: the picker list, exact UID checks, and readiness.
type identitySource interface {
	connections.IdentityCatalog
	auth.IdentityUIDReader
	ready() bool
}

// startIdentities follows Identities through PolicyService; stop ends it.
func startIdentities(ctx context.Context, c config) (identitySource, func(), error) {
	cache, stop, err := identitycache.Start(ctx, c.PolicyService, identitycache.StandardGrace)
	if err != nil {
		return nil, nil, err
	}
	return cachedIdentities{cache}, stop, nil
}

// cachedIdentities is the PolicyService-backed view; a stale view lists nothing and is not ready.
type cachedIdentities struct{ *identitycache.Cache }

func (i cachedIdentities) ConnectionIdentities() ([]auth.Identity, bool) {
	listed, err := i.List()
	return listed, err == nil
}

func (i cachedIdentities) ready() bool {
	_, err := i.List()
	if err != nil {
		slog.Warn("MCP edge Identity view unavailable", "error", err)
	}
	return err == nil
}
