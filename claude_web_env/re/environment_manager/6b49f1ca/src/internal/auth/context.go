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

// supabaseConfig holds Supabase credentials parsed from a "supabase" auth config Token field.
// Binary: NewAuthContextWithSessionID, JSON struct with 4 fields found at "supabase" case.
// JSON tags: project_ref, anon_key, db_pass, pat.
type supabaseConfig struct {
	ProjectRef string `json:"project_ref"`
	AnonKey    string `json:"anon_key"`
	DBPass     string `json:"db_pass"`
	PAT        string `json:"pat"`
}

// AuthContext holds authentication tokens for various providers.
//
// Binary struct layout (new binary b71486df):
//   offset 0x00: sessionIngressToken string (ptr+len)
//   offset 0x10: anthropicAPIToken string (ptr+len)
//   offset 0x20: anthropicOAuthToken string (ptr+len)
//   offset 0x30: vercelDeployToken string (ptr+len)
//   offset 0x40: supabaseProjectRef string (ptr+len)  ← new
//   offset 0x50: supabaseAnonKey string (ptr+len)     ← new
//   offset 0x60: supabaseDBPass string (ptr+len)      ← new
//   offset 0x70: supabasePAT string (ptr+len)         ← new
//   offset 0x80: sessionID string (ptr+len)           ← shifted from 0x40
//   offset 0x90: logger *slog.Logger
type AuthContext struct {
	sessionIngressToken string
	anthropicAPIToken   string
	anthropicOAuthToken string
	vercelDeployToken   string
	supabaseProjectRef  string
	supabaseAnonKey     string
	supabaseDBPass      string
	supabasePAT         string
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

		case "supabase":
			// Binary: NewAuthContextWithSessionID supabase case (new in b71486df).
			// Token field contains a JSON-encoded supabaseConfig struct.
			// Logs "Configured Supabase credentials" with project_ref, anon_key, db_pass, pat.
			if config.Token == "" {
				return nil, fmt.Errorf("supabase auth configuration missing required 'token' field")
			}
			var sbCfg supabaseConfig
			if err := json.Unmarshal([]byte(config.Token), &sbCfg); err != nil {
				return nil, fmt.Errorf("failed to parse supabase auth configuration: %w", err)
			}
			ctx.supabaseProjectRef = sbCfg.ProjectRef
			ctx.supabaseAnonKey = sbCfg.AnonKey
			ctx.supabaseDBPass = sbCfg.DBPass
			ctx.supabasePAT = sbCfg.PAT
			if ctx.logger != nil {
				ctx.logger.Info("Configured Supabase credentials",
					"project_ref", sbCfg.ProjectRef,
					"anon_key", sbCfg.AnonKey,
					"db_pass", sbCfg.DBPass,
					"pat", sbCfg.PAT,
				)
			}

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

// GetSupabaseProjectRef returns the Supabase project ref.
// Binary: context.go offset 0x40/0x48.
func (a *AuthContext) GetSupabaseProjectRef() string {
	return a.supabaseProjectRef
}

// GetSupabaseAnonKey returns the Supabase anon key.
// Binary: context.go offset 0x50/0x58.
func (a *AuthContext) GetSupabaseAnonKey() string {
	return a.supabaseAnonKey
}

// GetSupabaseDBPass returns the Supabase database password.
// Binary: context.go offset 0x60/0x68.
func (a *AuthContext) GetSupabaseDBPass() string {
	return a.supabaseDBPass
}

// GetSupabasePAT returns the Supabase personal access token.
// Binary: context.go offset 0x70/0x78.
func (a *AuthContext) GetSupabasePAT() string {
	return a.supabasePAT
}

// HasSupabase returns true if Supabase credentials are configured.
// Binary: checks supabaseProjectRef length (offset 0x48) != 0.
func (a *AuthContext) HasSupabase() bool {
	return a.supabaseProjectRef != ""
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
