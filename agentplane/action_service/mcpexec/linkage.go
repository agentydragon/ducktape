package mcpexec

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
)

// Linkages are the OAuth linkages operators made to MCP servers, which *linkage.Authority holds. Linking, refreshing
// and disconnecting an account are the linkage's, not the executor's.
type Linkages interface {
	// Token is what the linkage of the server serverID holds now; nil when no account is linked to it.
	Token(ctx context.Context, serverID string) (*linkage.Token, error)
}

// linkageProblem is why the server's linkage cannot authorize a session now, for an operator; empty when it can.
func linkageProblem(ctx context.Context, linkages Linkages, serverID string) (string, error) {
	token, err := linkages.Token(ctx, serverID)
	switch {
	case err != nil:
		return "", err
	case token == nil:
		return "no account is linked", nil
	case token.RefreshFailure != nil && *token.RefreshFailure == linkage.RefreshReconnect:
		return "refreshing the token failed in a way retrying cannot fix; link the account again", nil
	case expired(token):
		return fmt.Sprintf("the access token expired at %s and has not been refreshed",
			token.ExpiresAt.UTC().Format("2006-01-02 15:04:05 MST")), nil
	}
	return "", nil
}

// requestToken is the token the server's linkage holds for a request sent now.
func requestToken(ctx context.Context, linkages Linkages, serverID string) (string, error) {
	token, err := linkages.Token(ctx, serverID)
	if err != nil {
		return "", err
	}
	if token == nil || expired(token) {
		return "", errors.New("MCP server is not linked or its token has expired")
	}
	return token.AccessToken, nil
}

func expired(token *linkage.Token) bool {
	return token.ExpiresAt != nil && !token.ExpiresAt.After(time.Now())
}
