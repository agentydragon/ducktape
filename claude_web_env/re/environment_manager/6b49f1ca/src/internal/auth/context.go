package auth

import (
	"context"
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
// and logger populated.
func NewAuthContextWithSessionID(
	logger *slog.Logger,
	authJSON json.RawMessage,
	sessionID string,
) (*AuthContext, error) {
	if authJSON == nil {
		ctx := &AuthContext{
			sessionID: sessionID,
			logger:    logger,
		}
		return ctx, nil
	}

	var configs []AuthConfig
	if err := json.Unmarshal(authJSON, &configs); err != nil {
		return nil, fmt.Errorf("failed to parse auth configuration: %w", err)
	}

	ctx := &AuthContext{
		sessionID: sessionID,
		logger:    logger,
	}

	for _, config := range configs {
		switch config.Type {
		case "anthropic_api":
			if config.Token == "" {
				return nil, fmt.Errorf("anthropic_api auth configuration missing required 'token' field")
			}
			ctx.anthropicAPIToken = config.Token
			logger.Log(context.Background(), slog.LevelInfo, "Configured Anthropic API token",
				"type", config.Type,
			)

		case "vercel_deploy":
			if config.Token == "" {
				return nil, fmt.Errorf("vercel_deploy auth configuration missing required 'token' field")
			}
			ctx.vercelDeployToken = config.Token
			logger.Log(context.Background(), slog.LevelInfo, "Configured Vercel deploy token",
				"type", config.Type,
			)

		case "anthropic_oauth":
			if config.Token == "" {
				return nil, fmt.Errorf("anthropic_oauth auth configuration missing required 'token' field")
			}
			ctx.anthropicOAuthToken = config.Token
			logger.Log(context.Background(), slog.LevelInfo, "Configured Anthropic OAuth token",
				"type", config.Type,
			)

		case "session_ingress":
			if config.Token == "" {
				return nil, fmt.Errorf("session_ingress auth configuration missing required 'token' field")
			}
			ctx.sessionIngressToken = config.Token
			logger.Log(context.Background(), slog.LevelInfo, "Configured session ingress token",
				"type", config.Type,
			)

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
