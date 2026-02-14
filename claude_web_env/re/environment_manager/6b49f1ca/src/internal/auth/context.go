package auth

import (
	"encoding/json"
	"fmt"
	"log/slog"
)

// AuthConfig represents a single auth configuration entry from JSON.
type AuthConfig struct {
	Type  string `json:"type"`
	Token string `json:"token"`
}

// AuthContext holds authentication tokens for various providers.
type AuthContext struct {
	sessionIngressToken string
	anthropicAPIToken   string
	anthropicOAuthToken string
	vercelDeployToken   string
	sessionID           string
	logger              *slog.Logger
}

// NewAuthContextWithSessionID creates a new AuthContext by parsing the given
// auth JSON (a JSON array of AuthConfig objects) and setting the session ID.
// If authJSON is nil, an empty AuthContext is returned with only the session ID
// populated.
func NewAuthContextWithSessionID(
	authJSON json.RawMessage,
	sessionID string,
) (*AuthContext, error) {
	if authJSON == nil {
		ctx := &AuthContext{
			sessionID: sessionID,
		}
		return ctx, nil
	}

	var configs []AuthConfig
	if err := json.Unmarshal(authJSON, &configs); err != nil {
		return nil, fmt.Errorf("failed to parse auth configuration: %w", err)
	}

	ctx := &AuthContext{
		sessionID: sessionID,
	}

	for _, config := range configs {
		switch config.Type {
		case "anthropic_api":
			if config.Token == "" {
				return nil, fmt.Errorf("anthropic_api auth configuration missing required 'token' field")
			}
			ctx.anthropicAPIToken = config.Token

		case "vercel_deploy":
			if config.Token == "" {
				return nil, fmt.Errorf("vercel_deploy auth configuration missing required 'token' field")
			}
			ctx.vercelDeployToken = config.Token

		case "anthropic_oauth":
			if config.Token == "" {
				return nil, fmt.Errorf("anthropic_oauth auth configuration missing required 'token' field")
			}
			ctx.anthropicOAuthToken = config.Token

		case "session_ingress":
			if config.Token == "" {
				return nil, fmt.Errorf("session_ingress auth configuration missing required 'token' field")
			}
			ctx.sessionIngressToken = config.Token

		default:
			return nil, fmt.Errorf("unknown auth provider type: %s", config.Type)
		}
	}

	return ctx, nil
}

// GetSessionIngressToken returns the session ingress token.
func (a *AuthContext) GetSessionIngressToken() string {
	return a.sessionIngressToken
}

// GetAnthropicAPIToken returns the Anthropic API token.
func (a *AuthContext) GetAnthropicAPIToken() string {
	return a.anthropicAPIToken
}

// GetAnthropicOAuthToken returns the Anthropic OAuth token.
func (a *AuthContext) GetAnthropicOAuthToken() string {
	return a.anthropicOAuthToken
}

// GetVercelDeployToken returns the Vercel deploy token.
func (a *AuthContext) GetVercelDeployToken() string {
	return a.vercelDeployToken
}

// GetSessionID returns the session ID.
func (a *AuthContext) GetSessionID() string {
	return a.sessionID
}

// SetSessionID sets the session ID.
func (a *AuthContext) SetSessionID(sessionID string) {
	a.sessionID = sessionID
}

// SetSessionIngressToken sets the session ingress token.
func (a *AuthContext) SetSessionIngressToken(token string) {
	a.sessionIngressToken = token
}
