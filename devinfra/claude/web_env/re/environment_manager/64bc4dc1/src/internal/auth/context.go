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

// CLEANUP(2026-03-26): supabaseConfig and antspaceDeployConfig removed in 64bc4dc1.
// Supabase MCP server, Vercel deploy, and Antspace deploy were all excised.

// AuthContext holds authentication tokens for various providers.
//
// Binary struct layout (64bc4dc1, garble-obfuscated):
//
// Removed in 64bc4dc1 (vs a6f96673): vercelDeployToken, antspaceControlPlaneURL,
// antspaceAuthToken, supabaseProjectRef, supabaseAnonKey, supabaseDBPass, supabasePAT.
//
// Added in 64bc4dc1: jwt (json:"jwt" found in binary string table).
//
//	offset 0x00: sessionIngressToken string (ptr+len)
//	offset 0x10: anthropicAPIToken string (ptr+len)
//	offset 0x20: anthropicOAuthToken string (ptr+len)
//	offset 0x30: jwt string (ptr+len)                     ← added in 64bc4dc1
//	offset 0x40: sessionID string (ptr+len)
//	offset 0x50: logger *slog.Logger
type AuthContext struct {
	sessionIngressToken string
	anthropicAPIToken   string
	anthropicOAuthToken string
	jwt                 string // Binary: json:"jwt" tag found in 64bc4dc1 string table.
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

		case "jwt":
			// Binary: json:"jwt" tag found in 64bc4dc1 string table.
			// New auth type added in 64bc4dc1, replacing Vercel/Antspace/Supabase.
			if config.Token == "" {
				return nil, fmt.Errorf("jwt auth configuration missing required 'token' field")
			}
			ctx.jwt = config.Token

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

// GetJWT returns the JWT auth token.
// Binary: json:"jwt" tag found in 64bc4dc1. Added in 64bc4dc1, replacing
// Vercel/Antspace/Supabase auth fields.
func (a *AuthContext) GetJWT() string {
	return a.jwt
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
