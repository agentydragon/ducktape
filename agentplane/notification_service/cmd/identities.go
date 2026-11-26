package main

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/policy/identitycache"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
)

// identitySources is where every Identity reader in the process learns which Identity incarnations are live.
type identitySources struct {
	// uids backs the caller SVID resolver and the Sandbox reader.
	uids auth.IdentityUIDReader
	// rules backs the send-policy watches.
	rules reader.IdentitySource
}

// startIdentities follows Identities through PolicyService; stop ends that view.
func startIdentities(ctx context.Context, c config) (identitySources, func(), error) {
	cache, stop, err := identitycache.Start(ctx, c.PolicyService, identitycache.StandardGrace)
	if err != nil {
		return identitySources{}, nil, err
	}
	return identitySources{uids: cache, rules: reader.CachedIdentities{View: cache}}, stop, nil
}
