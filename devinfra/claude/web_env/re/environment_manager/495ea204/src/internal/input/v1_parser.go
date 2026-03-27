package input

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/api"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/auth"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/claude"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y"
)

// V1Parser parses V1-format input using the work response protocol.
//
// Binary: type:.eq at 0xb0c860
// Struct fields accessed in buildSessionResult (offsets 0x00, 0x18, 0x20, 0x30, 0x48, 0x50, 0x58):
//
//	0x00: Logger        *slog.Logger
//	0x08: SessionID     string (0x08 data, 0x10 len)
//	0x18: APIUrl        string (0x18 data, 0x20 len)
//	0x28: SecretKey     string (0x28 data, 0x30 len)
//	0x38: O11y          o11y.O11yService (interface: 0x38 itab, 0x40 data)
//	0x48: SecretPath    string (0x48 data, 0x50 len)
//	0x58: ConfigClient  *api.SessionsClient (0x58)
type V1Parser struct {
	Logger       *slog.Logger
	SessionID    string
	APIUrl       string
	SecretKey    string
	O11y         o11y.O11yService
	SecretPath   string
	ConfigClient *api.SessionsClient
}

// Parse parses V1-format input data and returns a ParsedContext.
func (p *V1Parser) Parse(data []byte) (*ParsedContext, error) {
	start := time.Now()
	logger := p.Logger

	logger.Info("Parsing V1 input format", "data_size_bytes", len(data))

	workResp, err := ParseV1WorkResponse(data)
	if err != nil {
		return nil, fmt.Errorf("failed to parse V1 work response: %w", err)
	}

	logger.Info("Parsing V1 input format",
		"data_size_bytes", workResp.ID,
		"content_preview", workResp.GetSessionID(),
		"data_size_bytes", workResp.Data.ID,
		"content_preview", workResp.Data.Type,
		"data_size_bytes", workResp.Data.SessionID,
	)

	secret, err := DecodeWorkSecret(workResp.Data.Secret)
	if err != nil {
		return nil, fmt.Errorf("failed to decode secret: %w", err)
	}

	authCtx, err := p.buildAuthContext(secret, workResp.GetSessionID())
	if err != nil {
		return nil, fmt.Errorf("failed to build auth context: %w", err)
	}

	if workResp.IsHealthcheck() {
		return p.buildHealthcheckResult(authCtx, secret, start)
	}

	return p.buildSessionResult(workResp, authCtx, secret, start)
}

// buildHealthcheckResult constructs a ParsedContext for a healthcheck work type.
func (p *V1Parser) buildHealthcheckResult(authCtx *auth.AuthContext, secret *WorkSecret, start time.Time) (*ParsedContext, error) {
	logger := p.Logger

	logger.Info("Processing healthcheck work type")

	elapsed := time.Since(start)
	logger.Info("Completed parsing V1 healthcheck input",
		"duration_ms", elapsed.Milliseconds(),
	)

	return &ParsedContext{
		AuthContext: authCtx,
	}, nil
}

// buildSessionResult constructs a ParsedContext for a session work type.
func (p *V1Parser) buildSessionResult(workResp *V1WorkResponse, authCtx *auth.AuthContext, secret *WorkSecret, start time.Time) (*ParsedContext, error) {
	logger := p.Logger

	logger.Info("Decoded work secret",
		"data_size_bytes", len(secret.Sources),
		"content_preview", workResp.GetSessionID(),
		"data_size_bytes", workResp.Data.SessionID,
		"content_preview", workResp.Data.Type,
	)

	sessionCtx, err := p.fetchSessionContext(workResp, authCtx, context.Background())
	if err != nil {
		return nil, fmt.Errorf("failed to fetch session context: %w", err)
	}

	startupCtx, err := p.buildStartupContext(sessionCtx, secret)
	if err != nil {
		return nil, fmt.Errorf("failed to build startup context: %w", err)
	}

	envResp, err := p.buildEnvironmentResponse(sessionCtx)
	if err != nil {
		return nil, fmt.Errorf("failed to build environment response: %w", err)
	}

	outcomes := p.buildOutcomes(sessionCtx, secret)

	elapsed := time.Since(start)
	logger.Info("Completed parsing V1 input",
		"duration_ms", elapsed.Milliseconds(),
	)

	return &ParsedContext{
		StartupContext:    startupCtx,
		EnvironmentConfig: envResp,
		AuthContext:       authCtx,
		Outcomes:          outcomes,
		SessionID:         workResp.GetSessionID(),
		WorkID:            workResp.ID,
		McpConfigFile:     sessionCtx.McpConfigFile,
	}, nil
}

// sessionContext represents the session context data from the API.
type sessionContext struct {
	StartupContext    json.RawMessage       `json:"startup_context"`
	EnvironmentConfig json.RawMessage       `json:"environment"`
	AuthConfigs       json.RawMessage       `json:"auth_configs"`
	Outcomes          json.RawMessage       `json:"outcomes"`
	McpConfigFile     *config.McpConfigFile `json:"mcp_config_file"`
}

// fetchSessionContext retrieves the session context from the Anthropic API.
//
// Binary: inlined into buildSessionResult at 0xb0b1cd-0xb0b2b6
//
// Reconstructed flow from disassembly:
//  1. Loads workResp.Data.SessionID (offsets 0x18, 0x20 of V1Parser param SI)
//     and p.Logger (offset 0x00 of V1Parser at AX) for api.NewHttpClient call
//  2. At 0xb0b1ea: calls api.NewHttpClient(sessionID_string, p.Logger, nil)
//     where the sessionID is used as the base URL. In practice the API URL
//     is stored elsewhere and the binary dereferences through the WorkSecret
//     and AuthContext structs to get the actual API key and endpoint.
//  3. At 0xb0b209-0xb0b22a: constructs a SessionsClient on the stack at
//     0xd8(SP) with fields:
//     - 0xd8: HttpClient    (from NewHttpClient return)
//     - 0xe0: ApiKey.ptr    (from authCtx[0x00] = sessionIngressToken ptr)
//     - 0xe8: ApiKey.len    (from authCtx[0x08] = sessionIngressToken len)
//     - 0xf0: Logger        (p.Logger)
//  4. At 0xb0b232-0xb0b25a: loads workResp.Data.SessionID (offsets 0x50, 0x58
//     of workResp) and calls GetSessionContext(ctx, sessionID)
//  5. On error (BX != nil at 0xb0b260): wraps with
//     "failed to fetch session context: %w" and returns
//  6. On success: returns the *api.SessionContext directly (the sessionContext
//     local type mirrors the API response structure)
func (p *V1Parser) fetchSessionContext(workResp *V1WorkResponse, authCtx *auth.AuthContext, ctx context.Context) (*sessionContext, error) {
	// Create HTTP client using the parser's API URL and logger.
	// Binary: 0xb0b1ea calls api.NewHttpClient
	httpClient := api.NewHttpClient(p.APIUrl, p.Logger, nil)

	// Construct a SessionsClient with the HTTP client and session ingress token.
	// Binary: struct built at 0xd8(SP) in buildSessionResult
	// The API key comes from authCtx's first field (sessionIngressToken).
	sessionsClient := &api.SessionsClient{
		Client: httpClient,
		ApiKey: authCtx.GetSessionIngressToken(),
		Logger: p.Logger,
	}

	// Fetch the session context from the API.
	// Binary: 0xb0b25a calls GetSessionContext with the session ID
	// extracted from workResp.Data.SessionID (offsets 0x50, 0x58 of workResp).
	sessionID := workResp.GetSessionID()
	resp, err := sessionsClient.GetSessionContext(ctx, sessionID)
	if err != nil {
		return nil, err
	}

	// Convert the API response to our local sessionContext type.
	// The API SessionContext and local sessionContext share the same JSON structure.
	respData, err := json.Marshal(resp)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal session context response: %w", err)
	}

	var sessCtx sessionContext
	if err := json.Unmarshal(respData, &sessCtx); err != nil {
		return nil, fmt.Errorf("failed to parse session context: %w", err)
	}

	return &sessCtx, nil
}

// buildStartupContext parses startup context from session context and merges with work secret sources.
func (p *V1Parser) buildStartupContext(sessCtx *sessionContext, secret *WorkSecret) (*config.StartupContext, error) {
	var startupCtx config.StartupContext

	if sessCtx.StartupContext != nil {
		if err := json.Unmarshal(sessCtx.StartupContext, &startupCtx); err != nil {
			return nil, fmt.Errorf("failed to parse outcomes from session: %w", err)
		}
	}

	// Merge sources from the work secret into the startup context
	for _, src := range secret.Sources {
		startupCtx.Sources = append(startupCtx.Sources, src)
	}

	return &startupCtx, nil
}

// buildEnvironmentResponse marshals the environment configuration from the session context.
func (p *V1Parser) buildEnvironmentResponse(sessCtx *sessionContext) (json.RawMessage, error) {
	if sessCtx.EnvironmentConfig == nil {
		return nil, nil
	}

	var envConfig config.EnvironmentConfig
	if err := json.Unmarshal(sessCtx.EnvironmentConfig, &envConfig); err != nil {
		return nil, fmt.Errorf("failed to marshal environment config: %w", err)
	}

	result, err := json.Marshal(&envConfig)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal environment config: %w", err)
	}

	return result, nil
}

// buildAuthContext creates an auth context from the work secret and session ID.
func (p *V1Parser) buildAuthContext(secret *WorkSecret, sessionID string) (*auth.AuthContext, error) {
	authData, err := json.Marshal(secret.Sources)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal auth configs: %w", err)
	}

	authCtx, err := auth.NewAuthContextWithSessionID(authData, sessionID)
	if err != nil {
		return nil, fmt.Errorf("failed to create auth context: %w", err)
	}

	return authCtx, nil
}

// buildOutcomes parses outcomes from the session context to populate git push information.
func (p *V1Parser) buildOutcomes(sessCtx *sessionContext, secret *WorkSecret) *claude.Outcomes {
	outcomes := claude.NewOutcomes()

	if sessCtx.Outcomes == nil {
		return outcomes
	}

	var rawOutcomes []v0Outcome
	if err := json.Unmarshal(sessCtx.Outcomes, &rawOutcomes); err != nil {
		p.O11y.Increment("outcomes_parse_failed", nil, nil)
		logger := p.Logger
		logger.Warn("Failed to parse outcomes for git push info",
			"error", err,
		)
		return outcomes
	}

	for _, outcome := range rawOutcomes {
		if outcome.Type != "git_repository" {
			continue
		}

		if outcome.RemoteURL == "" {
			continue
		}

		if len(outcome.Branches) == 1 {
			outcomes.Add(outcome.RemoteURL, outcome.Branches[0])
		} else {
			logger := p.Logger
			logger.Warn("Outcome skipped: exactly one branch must be specified",
				"data_size_bytes", outcome.RemoteURL,
				"content_preview", len(outcome.Branches),
			)
		}
	}

	outcomes.ValidateWithLogger(p.Logger)

	numRepos := outcomes.Len()
	numBranches := 0
	if repos := outcomes.Repositories(); repos != nil {
		numBranches = len(repos)
	}

	logger := p.Logger
	logger.Info("Parsed outcomes from V1 session context",
		"data_size_bytes", numRepos,
		"content_preview", numBranches,
	)

	return outcomes
}
