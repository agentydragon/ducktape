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

// startIdentities follows Identities through PolicyService when configured, else reads the Identity resources of the
// namespace from the API server; stop ends the PolicyService view.
func startIdentities(ctx context.Context, c config, deps kubeDeps) (identitySources, func(), error) {
	if c.PolicyService == nil {
		uids, err := auth.KubernetesIdentityUIDs(deps.Sandboxes, c.Namespace)
		if err != nil {
			return identitySources{}, nil, err
		}
		return identitySources{uids: uids, rules: reader.KubernetesIdentities{}}, func() {}, nil
	}
	cache, stop, err := identitycache.Start(ctx, *c.PolicyService, identitycache.StandardGrace)
	if err != nil {
		return identitySources{}, nil, err
	}
	return identitySources{uids: cache, rules: reader.CachedIdentities{View: cache}}, stop, nil
}
