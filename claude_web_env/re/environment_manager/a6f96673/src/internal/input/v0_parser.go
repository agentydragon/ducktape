package input

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/auth"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/claude"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
)

// ParsedContext holds all the parsed results from parsing input data.
type ParsedContext struct {
	StartupContext    *config.StartupContext
	EnvironmentConfig json.RawMessage
	AuthContext       *auth.AuthContext
	Outcomes          *claude.Outcomes
	SessionID         string
	WorkID            string
	McpConfigFile     *config.McpConfigFile
	HasMcpConfig      bool
}

// V0Parser parses V0-format input from stdin.
type V0Parser struct {
	Logger    *slog.Logger
	SessionID string
	O11y      interface{}
}

// v0Input represents the raw V0 input JSON structure.
type v0Input struct {
	StartupContext json.RawMessage `json:"startup_context"`
	Environment    json.RawMessage `json:"environment"`
	Auth           json.RawMessage `json:"auth"`
	Outcomes       []v0Outcome     `json:"outcomes"`
	McpConfig      *v0McpConfig    `json:"mcp_config"`
	McpConfigData  json.RawMessage `json:"mcp_config_data"`
}

type v0McpConfig struct {
	Data json.RawMessage `json:"data"`
}

type v0Outcome struct {
	Type       string   `json:"type"`
	Name       string   `json:"name"`
	RemoteURL  string   `json:"remote_url"`
	Branch     string   `json:"branch"`
	Branches   []string `json:"branches"`
	CommitHash string   `json:"commit_hash"`
}

// Parse parses V0-format stdin input and returns a ParsedContext.
func (p *V0Parser) Parse(data []byte) (*ParsedContext, error) {
	start := time.Now()
	logger := p.Logger

	logger.Info("Parsing V0 input format", "data_size_bytes", len(data))

	var input v0Input
	if err := json.Unmarshal(data, &input); err != nil {
		return nil, fmt.Errorf("failed to parse V0 stdin JSON: %w", err)
	}

	startupCtx, err := p.parseStartupContext(&input)
	if err != nil {
		return nil, fmt.Errorf("failed to parse startup_context: %w", err)
	}

	// Log pre-computed args if mcp_config is present
	if input.McpConfig != nil || len(input.McpConfigData) > 0 {
		hasMcpConfig := len(input.McpConfigData) > 0
		logger.Info("Received pre-computed args from sandbox-gateway",
			"data_size_bytes", len(data),
			"mcp_config", input.McpConfig,
			"data_size_bytes", hasMcpConfig,
		)

		if input.McpConfig != nil {
			// Decode the MCP config content
			decoded, err := base64.StdEncoding.DecodeString(string(input.McpConfig.Data))
			if err == nil {
				logger.Warn("Pre-computed MCP config file",
					"data_size_bytes", string(input.McpConfig.Data),
					"content_preview", string(decoded),
					"content_preview", string(decoded),
				)
			}
		}
	}

	envResult, err := p.parseEnvironment(&input)
	if err != nil {
		return nil, fmt.Errorf("failed to parse environment: %w", err)
	}

	authResult, err := p.parseAuth(&input)
	if err != nil {
		return nil, fmt.Errorf("failed to parse auth: %w", err)
	}

	outcomesResult := p.parseOutcomes(&input)

	elapsed := time.Since(start)
	logger.Info("Completed parsing V0 input",
		"duration_ms", elapsed.Milliseconds(),
	)

	return &ParsedContext{
		StartupContext:    startupCtx,
		EnvironmentConfig: envResult,
		AuthContext:       authResult,
		Outcomes:          outcomesResult,
		SessionID:         p.SessionID,
	}, nil
}

// parseStartupContext parses the startup_context field from V0 input.
func (p *V0Parser) parseStartupContext(input *v0Input) (*config.StartupContext, error) {
	if input.StartupContext == nil {
		return nil, nil
	}

	var ctx config.StartupContext
	if err := json.Unmarshal(input.StartupContext, &ctx); err != nil {
		return nil, fmt.Errorf("failed to unmarshal startup_context: %w", err)
	}

	if err := ctx.Validate(); err != nil {
		return nil, fmt.Errorf("invalid startup_context: %w", err)
	}

	// Ensure API base URL has https:// prefix
	if len(ctx.APIBaseURL) >= 7 {
		if strings.HasPrefix(ctx.APIBaseURL, "http://") || strings.HasPrefix(ctx.APIBaseURL, "https://") {
			// Already has a scheme
		} else {
			ctx.APIBaseURL = "https://" + ctx.APIBaseURL
		}
	}

	logger := p.Logger
	logger.Info("Parsed startup context from V0 input",
		"data_size_bytes", ctx.APIBaseURL,
		"session_id", ctx.SessionID,
		"data_size_bytes", ctx.Len(),
		"content_preview", ctx.NumSources(),
		"content_preview", ctx.NumLanguages(),
	)

	return &ctx, nil
}

// parseEnvironment parses the environment field from V0 input.
func (p *V0Parser) parseEnvironment(input *v0Input) (json.RawMessage, error) {
	if input.Environment == nil {
		return nil, fmt.Errorf("environment configuration is required")
	}

	var env config.EnvironmentConfig
	if err := json.Unmarshal(input.Environment, &env); err != nil {
		return nil, fmt.Errorf("failed to unmarshal environment: %w", err)
	}

	// Validate environment_type is present
	if env.EnvironmentType == "" {
		return nil, fmt.Errorf("environment configuration missing required 'environment_type' field")
	}

	// Validate cwd for anthropic type
	if env.EnvironmentType == "anthropic" && env.Cwd == "" {
		return nil, fmt.Errorf("cwd field is required in environment configuration")
	}

	// Marshal the environment config back to JSON for the result
	envJSON, err := json.Marshal(&env)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal environment config: %w", err)
	}

	result := &environmentResult{
		EnvironmentType: env.EnvironmentType,
		Config:          envJSON,
		HasInitScript:   env.InitScript != "",
		InitScript:      env.InitScript,
		HasMcpServers:   env.McpServers != nil,
	}

	logger := p.Logger
	logger.Info("Parsed environment from V0 input",
		"data_size_bytes", result.EnvironmentType,
		"content_preview", result.HasInitScript,
		"data_size_bytes", result.HasMcpServers,
	)

	return envJSON, nil
}

// parseAuth parses the auth field from V0 input.
func (p *V0Parser) parseAuth(input *v0Input) (*auth.AuthContext, error) {
	if input.Auth != nil {
		authCtx, err := auth.NewAuthContextWithSessionID(input.Auth, p.SessionID)
		if err != nil {
			return nil, fmt.Errorf("failed to create auth context from JSON: %w", err)
		}

		hasAuth := input.Auth != nil
		logger := p.Logger
		logger.Info("Parsed auth from V0 input",
			"data_size_bytes", hasAuth,
		)
		return authCtx, nil
	}

	authCtx, err := auth.NewAuthContextWithSessionID(nil, p.SessionID)
	if err != nil {
		return nil, fmt.Errorf("failed to create empty auth context: %w", err)
	}

	hasAuth := input.Auth != nil
	logger := p.Logger
	logger.Info("Parsed auth from V0 input",
		"data_size_bytes", hasAuth,
	)
	return authCtx, nil
}

// parseOutcomes parses outcome data from V0 input into an Outcomes instance.
func (p *V0Parser) parseOutcomes(input *v0Input) *claude.Outcomes {
	outcomes := claude.NewOutcomes()

	if len(input.Outcomes) == 0 {
		return outcomes
	}

	for _, outcome := range input.Outcomes {
		if outcome.Type != "git_repository" {
			continue
		}

		if outcome.RemoteURL == "" {
			continue
		}

		if len(outcome.Branches) == 1 {
			// Single branch: record it as a map entry
			outcomes.Add(outcome.RemoteURL, outcome.Branches[0])
		} else {
			// Multiple or zero branches: log a warning
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
	logger.Info("Parsed outcomes from V0 input",
		"data_size_bytes", numRepos,
		"content_preview", numBranches,
	)

	return outcomes
}

// environmentResult holds the parsed environment information.
type environmentResult struct {
	EnvironmentType string
	Config          json.RawMessage
	HasInitScript   bool
	InitScript      string
	HasMcpServers   bool
}
