package input

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/auth"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/claude"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y"
)

// V1Parser parses V1-format input using the work response protocol.
type V1Parser struct {
	Logger *slog.Logger
	SessionID string
	O11y   o11y.O11yService
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

	sessionCtx, err := p.fetchSessionContext(workResp)
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
	StartupContext    json.RawMessage          `json:"startup_context"`
	EnvironmentConfig json.RawMessage          `json:"environment"`
	AuthConfigs       json.RawMessage          `json:"auth_configs"`
	Outcomes          json.RawMessage          `json:"outcomes"`
	McpConfigFile     *config.McpConfigFile    `json:"mcp_config_file"`
}

// fetchSessionContext retrieves and parses the session context from a work response.
func (p *V1Parser) fetchSessionContext(workResp *V1WorkResponse) (*sessionContext, error) {
	// The session context is fetched from the API or embedded in the work response
	var ctx sessionContext
	// Implementation would involve an API call or decoding from work response fields
	return &ctx, nil
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
		p.O11y.Increment("outcomes_parse_failed")
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
