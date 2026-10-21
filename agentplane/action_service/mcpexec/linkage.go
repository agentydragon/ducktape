package mcpexec

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
)

// Linkages are the OAuth linkages operators made to MCP servers, which *store.Store reads. Linking, refreshing and
// disconnecting an account are the linkage flow's, not the executor's.
type Linkages interface {
	// MCPLinkage reads the linkage of the server serverID; nil when no account is linked to it.
	MCPLinkage(ctx context.Context, serverID string) (*store.MCPLinkage, error)
}

// linkageProblem is why the server's linkage cannot authorize a session now, for an operator; empty when it can.
func linkageProblem(ctx context.Context, linkages Linkages, serverID string) (string, error) {
	linkage, err := linkages.MCPLinkage(ctx, serverID)
	switch {
	case err != nil:
		return "", err
	case linkage == nil:
		return "no account is linked", nil
	case linkage.RefreshFailureAction != nil &&
		(*linkage.RefreshFailureAction == "reconnect" || *linkage.RefreshFailureAction == "operator_action"):
		return "refreshing the token failed in a way retrying cannot fix; link the account again", nil
	case expired(linkage):
		return fmt.Sprintf("the access token expired at %s and has not been refreshed",
			linkage.ExpiresAt.UTC().Format("2006-01-02 15:04:05 MST")), nil
	}
	return "", nil
}

// requestToken is the token the server's linkage holds for a request sent now.
func requestToken(ctx context.Context, linkages Linkages, serverID string) (string, error) {
	linkage, err := linkages.MCPLinkage(ctx, serverID)
	if err != nil {
		return "", err
	}
	if linkage == nil || expired(linkage) {
		return "", errors.New("MCP server is not linked or its token has expired")
	}
	return linkage.AccessToken, nil
}

func expired(linkage *store.MCPLinkage) bool {
	return linkage.ExpiresAt != nil && !linkage.ExpiresAt.After(time.Now())
}
