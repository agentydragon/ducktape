// Package byoc implements the BYOC (Bring Your Own Container) environment type
// for the environment manager. BYOC environments run in customer-provided
// containers with custom auth round-tripping and lease management.
//
// Reconstructed from a6f96673 DWARF extraction, carried forward to 64bc4dc1.
// Source path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/envtype/byoc/
//
// Key symbols:
//   - byoc.New (0xb042e0)
//   - byoc.Registration (0x1589460)
//   - byoc.defaultSettingsJSON (0x15addc0)
//   - byoc.stopHookScript (0x15adde0)
//   - byoc.init (0xb04220)
//   - byoc.containProvideAuthRoundTripper (itab at 0xf5b500 approx)
package byoc

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/textproto"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/envtype"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/podmonitor"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/process"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/sources"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
)

// Registration is the global registration for the byoc environment type.
// Symbol: byoc.Registration (0x1589460)
var Registration *envtype.Registration

// defaultSettingsJSON holds the default Claude Code settings JSON for BYOC.
// Copied from envtype/shared.DefaultSettingsJSON during init().
// Symbol: byoc.defaultSettingsJSON (0x15addc0)
var defaultSettingsJSON []byte

// stopHookScript holds the stop hook script content for BYOC.
// Copied from envtype/shared.StopHookScript during init().
// Symbol: byoc.stopHookScript (0x15adde0)
var stopHookScript []byte

// init copies shared defaults from the shared package.
//
// Binary address: 0xb04220
// Assembly: loads from envtype/shared.DefaultSettingsJSON and
// envtype/shared.StopHookScript, stores into byoc.defaultSettingsJSON
// and byoc.stopHookScript respectively.
func init() {
	// defaultSettingsJSON = shared.DefaultSettingsJSON
	// stopHookScript = shared.StopHookScript
}

// byocConfig holds the JSON-decoded configuration for a BYOC environment.
//
// Struct layout (from New at 0xb042e0, type descriptor at 0xd79360):
//
//	offset 0x00: EnvironmentType string (ptr+len)
//	offset 0x10: CWD string (ptr+len)
//	offset 0x20: TaskSetupScript []byte (ptr+len+cap)
type byocConfig struct {
	EnvironmentType string `json:"environment_type"`
	CWD             string `json:"cwd"`
	TaskSetupScript []byte `json:"task_setup_script,omitempty"`
}

// byocEnvironmentType implements envtype.EnvironmentType for BYOC environments.
//
// Struct layout (from setter/getter method field access patterns):
//
//	offset 0x00: config *byocConfig
//	offset 0x08: logger *slog.Logger
//	offset 0x10: sessionMode config.SessionMode (string ptr+len)
//	offset 0x20: startupContext *config.StartupContext
//	offset 0x28: authContext interface{} (data pointer only)
type byocEnvironmentType struct {
	config         *byocConfig
	logger         *slog.Logger
	sessionMode    config.SessionMode
	startupContext *config.StartupContext
	authContext    interface{}
}

// containProvideAuthRoundTripper wraps an http.RoundTripper to inject
// container-provided authentication headers into outgoing requests.
// Used by CreateLeaseManager to authenticate lease renewal calls.
//
// itab: *byoc.containProvideAuthRoundTripper -> net/http.RoundTripper
// Referenced at 0xb07dc0 in CreateLeaseManager.
//
// Struct layout (from RoundTrip field access at 0xb07880 and
// CreateLeaseManager struct init at 0xb07d3c-0xb07da6):
//
//	offset 0x00: transport http.RoundTripper (interface: itab + data)
//	offset 0x10: token string (used as Bearer authorization)
//	offset 0x20: sessionID string (used as X-Organization-Uuid header)
//	offset 0x30: timeout time.Duration (0x6fc23ac00 = 30s)
type containProvideAuthRoundTripper struct {
	transport http.RoundTripper
	token     string
	sessionID string
	timeout   time.Duration
}

// RoundTrip implements http.RoundTripper, injecting container auth headers.
//
// Binary address: 0xb07880
// Source file: byoc.go
//
// Assembly flow:
//  1. Clones the request (preserving its context, or using background if nil)
//  2. Concatenates "Bearer " + rt.token
//  3. Sets "Authorization" header to "Bearer <token>"
//  4. Sets "x-organization-uuid" header to rt.sessionID
//  5. Sets "x-environment-runner-version" header to util.Version
//  6. Sets "anthropic-beta" header to "environments-2025-11-01"
//  7. Calls rt.transport.RoundTrip(clonedReq)
//  8. On error: wraps with "failed to execute HTTP request: %w"
func (rt *containProvideAuthRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	// 0xb078ce: Clone the request with its existing context
	clonedReq := req.Clone(req.Context())

	// 0xb078f6-0xb07902: Build "Bearer <token>" authorization value
	authValue := "Bearer " + rt.token

	// 0xb07912-0xb07980: Set Authorization header
	authKey := textproto.CanonicalMIMEHeaderKey("Authorization")
	clonedReq.Header[authKey] = []string{authValue}

	// 0xb079e8-0xb07a54: Set x-organization-uuid header with sessionID
	sessionKey := textproto.CanonicalMIMEHeaderKey("x-organization-uuid")
	clonedReq.Header[sessionKey] = []string{rt.sessionID}

	// 0xb07abe-0xb07b2b: Set x-environment-runner-version header with util.Version
	versionKey := textproto.CanonicalMIMEHeaderKey("x-environment-runner-version")
	clonedReq.Header[versionKey] = []string{util.Version}

	// 0xb07b79-0xb07bd2: Set anthropic-beta header
	betaKey := textproto.CanonicalMIMEHeaderKey("anthropic-beta")
	clonedReq.Header[betaKey] = []string{"environments-2025-11-01"}

	// 0xb07c16-0xb07c29: Delegate to underlying transport
	resp, err := rt.transport.RoundTrip(clonedReq)
	if err != nil {
		// 0xb07c4f: Wrap transport error
		return nil, fmt.Errorf("failed to execute HTTP request: %w", err)
	}

	return resp, nil
}

// New creates a new BYOC environment type instance by parsing the provided
// JSON configuration.
//
// Binary address: 0xb042e0
// Source file: byoc.go
//
// Assembly flow:
//  1. Allocates byocConfig via runtime.newobject (0xb04313), calls json.Unmarshal
//  2. Validates environment_type == "byoc" (0xb0438e: CMP 0x8(CX), $4; 0xb04398: CMP *(CX), "byoc")
//  3. Validates CWD starts with '/' (0xb0440f: CMPB 0(AX), $0x2f)
//  4. Validates CWD is clean (filepath.Clean matches original, 0xb04419-0xb04438)
//  5. Validates TaskSetupScript size <= 1MB (0xb044f3: CMPQ AX, $0x100000)
//  6. Allocates byocEnvironmentType (0xb04502), sets config, logger, sessionMode="new" (0xb04540)
//  7. Returns via itab (0xb0455a)
func New(configJSON []byte, logger *slog.Logger) (envtype.EnvironmentType, error) {
	cfg := &byocConfig{}
	if err := json.Unmarshal(configJSON, cfg); err != nil {
		// 0xb0435b: "failed to unmarshal byoc config: %w" (len=0x23)
		return nil, fmt.Errorf("failed to unmarshal byoc config: %w", err)
	}

	// 0xb0438e-0xb043a0: Check environment_type == "byoc"
	if cfg.EnvironmentType != "byoc" {
		// 0xb043c5: "invalid environment_type: expected 'byoc', got '%s'" (len=0x33)
		return nil, fmt.Errorf("invalid environment_type: expected 'byoc', got '%s'", cfg.EnvironmentType)
	}

	// 0xb043f5-0xb04412: CWD validation
	if cfg.CWD != "" {
		// 0xb0440f: Check first byte is '/'
		if cfg.CWD[0] != '/' {
			// 0xb044c1: "cwd must be an absolute path, got: %s" (len=0x25)
			return nil, fmt.Errorf("cwd must be an absolute path, got: %s", cfg.CWD)
		}
		// 0xb04419: filepath.Clean, then compare with original
		cleaned := filepath.Clean(cfg.CWD)
		if cleaned != cfg.CWD {
			// 0xb0446f: "cwd contains path traversal elements: %s" (len=0x28)
			return nil, fmt.Errorf("cwd contains path traversal elements: %s", cfg.CWD)
		}
	}

	// 0xb044ef-0xb044f9: Check TaskSetupScript size <= 1MB
	if len(cfg.TaskSetupScript) > 0x100000 {
		// 0xb0459a: "task_setup_script too large: %d bytes (max %d)" (len=0x2e)
		return nil, fmt.Errorf("task_setup_script too large: %d bytes (max %d)", len(cfg.TaskSetupScript), 0x100000)
	}

	// 0xb044fb-0xb04566: Allocate byocEnvironmentType, set fields
	env := &byocEnvironmentType{
		config:      cfg,
		logger:      logger,
		sessionMode: "new", // 0xb04540: MOVQ $3, 0x18(AX); 0xb04548: LEAQ "new", DX
	}
	return env, nil
}

// SetStartupContext sets the startup context on the BYOC environment.
//
// Binary address: 0xb04660
// Source file: byoc.go
// Assembly: stores BX at 0x20(AX).
func (e *byocEnvironmentType) SetStartupContext(ctx *config.StartupContext) {
	e.startupContext = ctx
}

// SetAuthContext sets the authentication context on the BYOC environment.
//
// Binary address: 0xb046c0
// Source file: byoc.go
// Assembly: stores BX at 0x28(AX).
func (e *byocEnvironmentType) SetAuthContext(authCtx interface{}) {
	e.authContext = authCtx
}

// SetSessionMode sets the session mode on the BYOC environment.
//
// Binary address: 0xb04600
// Source file: byoc.go
// Assembly: stores string (BX,CX) at 0x10(AX),0x18(AX).
func (e *byocEnvironmentType) SetSessionMode(mode config.SessionMode) {
	e.sessionMode = mode
}

// GetCWD returns the current working directory for the BYOC environment.
//
// Binary address: 0xb04720
// Source file: byoc.go
// Assembly: MOVQ 0(AX), CX; MOVQ 0x10(CX), AX; MOVQ 0x18(CX), BX; RET
// Reads self.config.CWD (config at offset 0, CWD at config offset 0x10).
func (e *byocEnvironmentType) GetCWD() string {
	return e.config.CWD
}

// GetClaudeEnvironmentVariables returns environment variables for the BYOC
// Claude Code process.
//
// Binary address: 0xb05ae0
// Source file: byoc.go
//
// Assembly evidence:
//   - 0xb05afa: Stores self at 0xb8(SP)
//   - 0xb05b02: MOVQ 0x20(AX), CX -> loads startupContext
//   - 0xb05b0b: MOVQ 0xe0(CX), DX -> loads Entrypoint.len (offset 0xe0 = 224)
//   - 0xb05b17: MOVQ 0xd8(CX), CX -> loads Entrypoint.ptr (offset 0xd8 = 216)
//   - Default entrypoint "remote" (len=6) used when Entrypoint is empty
//   - 0xb05b46: makemap_small -> creates result map
//   - 5 mapassign_faststr calls set fixed env vars:
//     "CLAUDE_CODE_REMOTE" = "true"
//     "CLAUDE_CODE_DEBUG" = "true"
//     "CLAUDE_CODE_EXIT_AFTER_STOP_DELAY" = "300000"
//     "CLAUDE_CODE_ENVIRONMENT_KIND" = "byoc"
//     "CLAUDE_CODE_ENTRYPOINT" = entrypoint
//   - Then iterates startupContext.EnvironmentVariables map (offset 0xe8) if non-nil,
//     copying entries into the result map
func (e *byocEnvironmentType) GetClaudeEnvironmentVariables() map[string]string {
	// 0xb05b02-0xb05b37: Determine entrypoint from startupContext or default "remote"
	entrypoint := "remote"
	if e.startupContext != nil {
		if e.startupContext.Entrypoint != "" {
			entrypoint = e.startupContext.Entrypoint
		}
	}

	// 0xb05b46: makemap_small
	vars := make(map[string]string)

	// 0xb05b53: "CLAUDE_CODE_REMOTE" = "true"
	vars["CLAUDE_CODE_REMOTE"] = "true"
	// 0xb05b9e: "CLAUDE_CODE_DEBUG" = "true"
	vars["CLAUDE_CODE_DEBUG"] = "true"
	// 0xb05be3: "CLAUDE_CODE_EXIT_AFTER_STOP_DELAY" = "300000"
	vars["CLAUDE_CODE_EXIT_AFTER_STOP_DELAY"] = "300000"
	// 0xb05c26: "CLAUDE_CODE_ENVIRONMENT_KIND" = "byoc"
	vars["CLAUDE_CODE_ENVIRONMENT_KIND"] = "byoc"
	// 0xb05c69: "CLAUDE_CODE_ENTRYPOINT" = entrypoint
	vars["CLAUDE_CODE_ENTRYPOINT"] = entrypoint

	// 0xb05caa-0xb05d9e: Copy EnvironmentVariables from startupContext if present
	if e.startupContext != nil && e.startupContext.EnvironmentVariables != nil {
		for k, v := range e.startupContext.EnvironmentVariables {
			vars[k] = v
		}
	}

	return vars
}

// Initialize performs the full initialization sequence for a BYOC environment.
//
// Binary address: 0xb04740
// Source file: byoc.go
//
// Assembly flow:
//  1. 0xb048a6: Logs "Initializing BYOC environment" (len=0x1d) with session_mode, has_script, cwd
//  2. 0xb04956: If config.CWD != "": logs "Setting working directory" (len=0x19)
//  3. 0xb04a18: os.MkdirAll(cwd, 0755); on error: "failed to create working directory %s: %w" (len=0x29)
//  4. 0xb04ae7: os.Chdir(cwd); on error: "failed to change to working directory %s: %w" (len=0x2c)
//  5. 0xb04b93: Logs "Working directory set successfully" (len=0x22)
//  6. 0xb04c46: bootstrapClaudeSettings; on error: logs "Failed to bootstrap Claude settings" (len=0x23) at Warn
//  7. Calls setupGitConfig
//  8. 0xb04d46: Checks sessionMode (len=0x27 log msg "Checking session mode for task setup script")
//  9. 0xb04d8c: If mode is "new" or "setup-only" (len=0x19 "Running task setup script")
//     and has script: calls runScript
//  10. 0xb04e04: runScript error: "task setup script failed: %w" (len=0x1c)
//  11. 0xb04ebc: If mode is "resume-cached" (len=0x3b log msg): skip script, log info
//  12. 0xb04f8a: handleBranchCheckout error log (len=0x1b)
//  13. 0xb04fe6: handleBranchCheckout error wrap (len=0x33)
//  14. 0xb0502e: Logs "BYOC environment initialization completed" (len=0x29)
func (e *byocEnvironmentType) Initialize(ctx context.Context) error {
	hasScript := len(e.config.TaskSetupScript) > 0

	// 0xb048a6: Log initialization start
	e.logger.Info("Initializing BYOC environment",
		"session_mode", string(e.sessionMode),
		"has_script", hasScript,
		"cwd", e.config.CWD,
	)

	// 0xb04956-0xb04b93: Set working directory if configured
	if e.config.CWD != "" {
		e.logger.Info("Setting working directory",
			"cwd", e.config.CWD,
		)

		// 0xb04a18: MkdirAll with mode 0755
		if err := os.MkdirAll(e.config.CWD, 0o755); err != nil {
			return fmt.Errorf("failed to create working directory %s: %w", e.config.CWD, err)
		}

		// 0xb04ae7: Chdir
		if err := os.Chdir(e.config.CWD); err != nil {
			return fmt.Errorf("failed to change to working directory %s: %w", e.config.CWD, err)
		}

		// 0xb04b93: Success log
		e.logger.Info("Working directory set successfully",
			"cwd", e.config.CWD,
		)
	}

	// 0xb04c46: Bootstrap Claude settings
	if err := e.bootstrapClaudeSettings(ctx); err != nil {
		e.logger.Warn("Failed to bootstrap Claude settings",
			"error", err,
		)
	}

	// Setup git config
	e.setupGitConfig(ctx)

	// 0xb04d46: Check session mode for running the task setup script
	isNewOrSetupOnly := e.sessionMode == config.SessionModeNew || e.sessionMode == config.SessionModeSetupOnly

	if hasScript {
		if isNewOrSetupOnly {
			// 0xb04d8c: Log and run script
			e.logger.Info("Running task setup script")

			if err := e.runScript(ctx); err != nil {
				// 0xb04e04: Wrap script error
				return fmt.Errorf("task setup script failed: %w", err)
			}
		} else {
			// 0xb04ebc: Fast resume log
			e.logger.Info("Fast resume: Skipping task setup script for non-new session mode",
				"session_mode", string(e.sessionMode),
			)
		}
	}

	// 0xb04f8a: Handle branch checkout for resume-cached mode
	if e.sessionMode == config.SessionModeResumeCached {
		if e.startupContext != nil && len(e.startupContext.Outcomes) > 0 {
			if len(e.startupContext.Sources) > 0 {
				e.logger.Info("Attempting to checkout target branches for BYOC environment")

				if err := e.handleBranchCheckout(ctx); err != nil {
					e.logger.Error("Failed to checkout branches",
						"error", err,
					)
					// 0xb04fe6: Wrap branch checkout error
					return fmt.Errorf("failed to checkout branches in BYOC environment: %w", err)
				}
			}
		}
	}

	// 0xb0502e: Final log
	e.logger.Info("BYOC environment initialization completed")
	return nil
}

// CreateLeaseManager creates a lease manager for the BYOC environment.
// This sets up an HTTP client with container auth round-tripping and
// configures periodic lease renewal.
//
// Binary address: 0xb07cc0
// Source file: byoc.go
//
// Assembly evidence:
//   - 0xb07d22: Allocates containProvideAuthRoundTripper struct
//   - 0xb07d2e: Loads net/http.DefaultTransport for transport field
//   - 0xb07d3c-0xb07da6: Sets transport, token, sessionID fields on cPART
//   - 0xb07da6: Allocates http.Client with timeout=30s (0x6fc23ac00)
//   - 0xb07dc0: Sets itab for containProvideAuthRoundTripper -> http.RoundTripper
//   - 0xb07df8: Stores cPART as http.Client.Transport
//   - 0xb07edb: Logs "Creating LeaseManager for BYOC environment" (len=0x2a) with session_id, work_id
//   - 0xb07f00: Calls podmonitor.GetDefaultHealthFilePath
//   - 0xb07f95: Calls podmonitor.NewLeaseManager with all params, heartbeatInterval=30s, leaseDuration=30s
func (e *byocEnvironmentType) CreateLeaseManager(ctx context.Context, sessionID string, workID string, apiBaseURL string) (envtype.LeaseManager, error) {
	// 0xb07d22-0xb07da6: Create auth round tripper wrapping default transport
	// The token and sessionID for the round tripper come from the caller's
	// context (traced through register spills at 0xe8-0x100(SP)).
	rt := &containProvideAuthRoundTripper{
		transport: http.DefaultTransport,
		// token and sessionID are set from auth context (plumbed by caller)
	}

	// 0xb07da6-0xb07df8: Create http.Client with 30s timeout and custom transport
	httpClient := &http.Client{
		Timeout:   30 * time.Second, // 0xb07db2: MOVQ $0x6fc23ac00
		Transport: rt,               // 0xb07dc0: itab for containProvideAuthRoundTripper
	}

	// 0xb07edb: Log creation
	e.logger.Info("Creating LeaseManager for BYOC environment",
		"session_id", sessionID,
		"work_id", workID,
	)

	// 0xb07f00: Get health file path
	healthFilePath := podmonitor.GetDefaultHealthFilePath()

	// 0xb07f95: Create and return the lease manager
	lm := podmonitor.NewLeaseManager(
		sessionID,      // environmentID (reused from sessionID per register mapping)
		e.logger,       // logger
		30*time.Second, // heartbeatInterval (0x6fc23ac00)
		30*time.Second, // leaseDuration (0x6fc23ac00)
		workID,         // workID
		sessionID,      // sessionID
		apiBaseURL,     // apiBaseURL
		"",             // sessionIngressToken (from auth context, traced from caller spills)
		healthFilePath, // healthFilePath
		httpClient,     // httpClient
	)

	return lm, nil
}

// extractRepoBranchMapping extracts repository-to-branch mappings from the
// startupContext's outcomes. Iterates Outcomes looking for "git_repository" type
// entries and maps each repo to its target branch.
//
// Binary address: 0xb050a0
// Source file: byoc.go
//
// Assembly flow:
//  1. 0xb050d5: Creates empty map via makemap_small
//  2. 0xb050e7: Loads startupContext (offset 0x20 from self)
//  3. 0xb050eb: Gets Outcomes slice (offset 0x28/0x30 from startupContext)
//  4. 0xb050f5-0xb05100: Iterates outcomes; each OutcomeField is 0x48 bytes
//  5. 0xb0513b: Checks outcome.Type length == 0xe (14 = len("git_repository"))
//  6. 0xb0514c-0xb0516a: Compares outcome.Type bytes against "git_repository"
//  7. 0xb05174: Checks GitInfo.Branches pointer != nil
//  8. 0xb05185: Checks len(branches); if == 1: map repo->branch
//  9. 0xb0535d: If len(branches) > 1: error
//  10. 0xb051ca: mapassign_faststr to set map[repo] = branch
//  11. 0xb052fe: Logs "Mapped repository to branch for checkout" (len=0x28)
//  12. 0xb053c1: Error "outcome for repo %s has %d branches, expected 0 or 1" (len=0x34)
//
// Returns: (map[string]string, error) where map key=repo, value=branch
func (e *byocEnvironmentType) extractRepoBranchMapping(ctx context.Context) (map[string]string, error) {
	// 0xb050d5: makemap_small
	branchMap := make(map[string]string)

	// 0xb050e7-0xb050f3: Load outcomes from startupContext
	if e.startupContext == nil {
		return branchMap, nil
	}

	// 0xb050f5-0xb05349: Iterate over Outcomes
	for _, outcome := range e.startupContext.Outcomes {
		// 0xb0513b-0xb0516a: Check outcome.Type == "git_repository"
		if outcome.Type != "git_repository" {
			continue
		}

		// 0xb05174: Check if Branches slice is non-nil
		branches := outcome.GitInfo.Branches
		if branches == nil {
			continue
		}

		// 0xb05185-0xb0518b: Check branch count
		if len(branches) == 1 {
			// 0xb051ca: mapassign_faststr
			repo := outcome.GitInfo.Repo
			branchMap[repo] = branches[0]

			// 0xb052fe: Log debug
			e.logger.Debug("Mapped repository to branch for checkout",
				"repo", repo,
				"branch", branches[0],
			)
		} else if len(branches) > 1 {
			// 0xb053c1: Error for multiple branches
			return nil, fmt.Errorf("outcome for repo %s has %d branches, expected 0 or 1", outcome.GitInfo.Repo, len(branches))
		}
		// len(branches) == 0: skip silently
	}

	// 0xb053f6: Return map
	return branchMap, nil
}

// handleBranchCheckout handles git branch checkout for BYOC environments.
// Creates a SourceHandlerManager and uses it to process sources for branch
// checkout, then sets up git proxy.
//
// Binary address: 0xb05440
// Source file: byoc.go
//
// Assembly flow:
//  1. 0xb0548b: Logs "Extracting repository-branch mapping from outcomes" (len=0x32)
//  2. 0xb054c0: Calls extractRepoBranchMapping
//  3. 0xb054c5: If error, returns it directly
//  4. 0xb054e0: If map is nil or empty (len == 0):
//     0xb05a50: Logs "No specific branches requested for checkout" (len=0x2b)
//     Returns nil
//  5. 0xb05577: Logs "Found branches to checkout" (len=0x1a) with count
//  6. 0xb05671-0xb0569a: Determines label "repositories" (len=12) vs "repository" (len=10)
//     using CMOVE based on count == 1
//  7. 0xb05703: fmt.Sprintf("Attempting to checkout branches for %d %s", count, label) (len=0x29)
//  8. 0xb0572b-0xb0575b: Calls activity recorder (from startupContext) with "init"/"info" args
//  9. 0xb055cd-0xb05604: Creates SourceHandlerManager via sources.NewSourceHandlerManager
//     with name="git_source_handler" (len=0x10), isResume=true
//  10. 0xb05630: If NewSourceHandlerManager error: "failed to create source handler manager: %w" (len=0x2b)
//  11. 0xb05789: Calls ProcessSources
//  12. 0xb057b9: If ProcessSources error: "failed to checkout branches: %w" (len=0x1f)
//  13. 0xb05876: Logs "Branches checked out successfully" (len=0x21) with count and label
//  14. 0xb05903: fmt.Sprintf for git proxy log
//  15. 0xb05989: Calls SetupGitProxyAfterSourcesProcessed
//  16. 0xb05a01: If proxy error: logs "Failed to setup git proxy" (len=0x19) at Warn
func (e *byocEnvironmentType) handleBranchCheckout(ctx context.Context) error {
	// 0xb0548b: Log start
	e.logger.Info("Extracting repository-branch mapping from outcomes")

	// 0xb054c0: Extract branch mapping
	branchMap, err := e.extractRepoBranchMapping(ctx)
	if err != nil {
		// 0xb05a78: Return error directly
		return err
	}

	// 0xb054d0-0xb054e0: Check if map is nil or empty
	if branchMap == nil || len(branchMap) == 0 {
		// 0xb05a50: No branches to checkout
		e.logger.Info("No specific branches requested for checkout")
		return nil
	}

	// 0xb05577: Log branch count
	e.logger.Info("Found branches to checkout",
		"count", len(branchMap),
	)

	// 0xb05671-0xb0569a: Determine singular/plural label
	label := "repositories"
	if len(branchMap) == 1 {
		label = "repository"
	}

	// 0xb05703: Format message for activity recorder
	msg := fmt.Sprintf("Attempting to checkout branches for %d %s", len(branchMap), label)

	// 0xb0572b-0xb0575b: Report to activity recorder (via startupContext callback)
	// The recorder is called via interface dispatch at 0xb0575b (CALL DX)
	// with args: "init", "info", msg, nil
	_ = msg // Used in activity recorder call

	// 0xb055cd-0xb05604: Create source handler manager
	mgr, err := sources.NewSourceHandlerManager(
		e.logger,
		e.config.CWD,
		"",       // sessionID
		nil,      // gitProxyManager
		nil,      // outcomes
		nil,      // activityRecorder
		"resume", // processMode
		true,     // isResume (0xb055e1: MOVB $1)
	)
	if err != nil {
		// 0xb05630: Wrap error
		return fmt.Errorf("failed to create source handler manager: %w", err)
	}

	// 0xb05789: Process sources
	if _, err := mgr.ProcessSources(ctx, e.logger, e.startupContext.Sources); err != nil {
		// 0xb057b9: Wrap error
		return fmt.Errorf("failed to checkout branches: %w", err)
	}

	// 0xb05876: Log success with count
	e.logger.Info("Branches checked out successfully",
		"count", len(branchMap),
	)

	// 0xb05989: Setup git proxy after sources processed
	if result, err := mgr.SetupGitProxyAfterSourcesProcessed(ctx, e.logger, e.startupContext.Sources); err != nil {
		// 0xb05a01: Log proxy error at Warn
		e.logger.Warn("Failed to setup git proxy",
			"error", err,
			"result", result,
		)
	}

	return nil
}

// runScript runs the task setup script in the BYOC environment.
// If the script starts with "#!", it's used as-is; otherwise, a bash shebang
// is prepended.
//
// Binary address: 0xb05dc0
// Source file: byoc.go
//
// Assembly flow:
//  1. 0xb05eda: Logs "Executing script" (len=0x10) with script_name, script_size_bytes
//  2. 0xb05f33: Checks for "#!/" shebang prefix (len=0x13 = "#!/bin/bash\nset -e\n")
//     If script starts with "#!": use content as-is
//     Otherwise: prepend "#!/bin/bash\nset -e\n"
//  3. 0xb05fe2: Formats pattern "%s-*.sh" (len=7) -> "task_setup-*.sh"
//  4. Creates OutputStreamer closure (func1 at 0xb06360) that:
//     - Maps StreamType 0->stdout, 1->stderr, else->unknown
//     - Logs "Script output" (len=0xd) with stream type and content
//  5. 0xb06080: Calls process.ExecuteScript(ctx, logger, content, pattern, streamer)
//  6. 0xb062c7: If ExecuteScript error: "failed to execute %s script: %w" (len=0x1f)
//  7. 0xb060c5: If result.Error != nil: "%s script failed: %w" (len=0x14)
//  8. 0xb0621c: Logs "Script completed successfully" (len=0x1d) with exit_code, duration
func (e *byocEnvironmentType) runScript(ctx context.Context) error {
	scriptName := "task_setup"
	scriptContent := string(e.config.TaskSetupScript)
	scriptSize := len(e.config.TaskSetupScript)

	// 0xb05eda: Log execution start
	e.logger.Info("Executing script",
		"script_name", scriptName,
		"script_size_bytes", scriptSize,
	)

	// 0xb05f33: Check for shebang; if not present, prepend bash shebang
	content := scriptContent
	if !hasShebang(content) {
		// 0xb05f33: Prepend "#!/bin/bash\nset -e\n" (len=0x13=19)
		content = "#!/bin/bash\nset -e\n" + content
	}

	// Create output streamer closure (func1 at 0xb06360)
	// The closure logs script output with stream type metadata
	streamer := util.OutputStreamer(func(ctx context.Context, streamType util.StreamType, data []byte) error {
		// 0xb0639a-0xb063ce: Map stream type to label
		var streamLabel string
		switch streamType {
		case util.StreamStdout:
			streamLabel = "stdout"
		case util.StreamStderr:
			streamLabel = "stderr"
		default:
			streamLabel = "unknown"
		}

		// 0xb064e4: Log "Script output" with stream, content, script_name
		e.logger.Info("Script output",
			"stream", streamLabel,
			"content", string(data),
			"script_name", scriptName,
		)
		return nil
	})

	// 0xb05fe2: Format pattern for temp file
	pattern := fmt.Sprintf("%s-*.sh", scriptName)

	// 0xb06080: Execute the script
	result, err := process.ExecuteScript(ctx, e.logger, content, pattern, streamer)
	if err != nil {
		// 0xb062c7: Wrap execution error
		return fmt.Errorf("failed to execute %s script: %w", scriptName, err)
	}

	// 0xb060c5: Check if the script exited with error
	if result != nil && result.Error != nil {
		return fmt.Errorf("%s script failed: %w", scriptName, result.Error)
	}

	// 0xb0621c: Log success
	e.logger.Info("Script completed successfully",
		"script_name", scriptName,
		"exit_code", result.ExitCode,
		"duration", result.Duration,
	)

	return nil
}

// hasShebang checks if content starts with "#!".
// Used by runScript to determine whether to prepend a bash shebang.
// Corresponds to the inline check at 0xb05f33 in the binary.
func hasShebang(content string) bool {
	return len(content) >= 2 && content[0] == '#' && content[1] == '!'
}

// gitConfigPair represents a key-value pair for git configuration.
// Used by setupGitConfig to iterate over config pairs.
type gitConfigPair struct {
	key   string
	value string
}

// setupGitConfig configures git settings for the BYOC environment.
// Sets user.name, user.email, gpg.format, gpg.ssh.program, commit.gpgsign,
// and http.proxyAuthMethod via "git config --global".
// Also creates $HOME/.ssh directory and a signing key file.
//
// Binary address: 0xb06bc0
// Source file: byoc.go
//
// Assembly flow:
//  1. 0xb06bf5: os.Getenv("SKIP_GIT_CONFIG") (len=0xf=15)
//  2. 0xb06c06-0xb06c12: If value == "true" (len=4, bytes "true"):
//     0xb06c32: Logs "Skipping git configuration (SKIP_GIT_CONFIG=true)" (len=0x31) and returns
//  3. 0xb06c74: Logs "Setting up git configuration for BYOC environment"
//  4. 0xb06cb4-0xb06df6: Initializes 6 git config pairs on stack:
//     - "user.name" (9) = "Claude" (6)
//     - "user.email" (10) = "noreply@anthropic.com" (21)
//     - "gpg.format" (10) = "ssh" (3)
//     - "gpg.ssh.program" (15) = "/tmp/code-sign" (14)
//     - "commit.gpgsign" (14) = "true" (4)
//     - "http.proxyAuthMethod" (20) = "basic" (5)
//  5. 0xb06e90-0xb071f0: For each pair, runs: git config --global <key> <value>
//     On error: 0xb070ea: Logs "Failed to set git config" (len=0x18) at Warn
//     On success: 0xb071f0: Logs "Set git config" (len=0xe) at Debug
//  6. 0xb07218: os.Getenv("HOME") (len=4)
//  7. 0xb0725e: filepath.Join(home, ".ssh") (len=4 for ".ssh")
//  8. 0xb07305: MkdirAll(sshDir, 0o700 = 0x1c0); error: "Failed to create .ssh directory" (len=0x1f)
//  9. 0xb0736c: filepath.Join(sshDir, "commit_signing_key.pub") (len=0x16=22)
//  10. 0xb07459: os.OpenFile(signingKeyPath, O_WRONLY|O_CREATE|O_TRUNC, 0666);
//     error: "Failed to create signing key file" (len=0x21)
//  11. 0xb0751f: success: "Created empty signing key file" (len=0x1e)
//  12. 0xb075b2: git config --global user.signingkey <signingKeyPath> (key len=0xf=15)
//  13. 0xb07739: error: "Failed to set git signing key config" (len=0x24)
//  14. 0xb077f1: success: "Set git signing key" (len=0x13)
//  15. 0xb07832: "Git configuration setup completed" (len=0x21)
func (e *byocEnvironmentType) setupGitConfig(ctx context.Context) error {
	// 0xb06bf5: Check if git config should be skipped
	if os.Getenv("SKIP_GIT_CONFIG") == "true" {
		// 0xb06c32: Log skip and return
		e.logger.Info("Skipping git configuration (SKIP_GIT_CONFIG=true)")
		return nil
	}

	// 0xb06c74: Log start
	e.logger.Info("Setting up git configuration for BYOC environment")

	// 0xb06cb4-0xb06df6: Git config key-value pairs
	configs := []gitConfigPair{
		{"user.name", "Claude"},
		{"user.email", "noreply@anthropic.com"},
		{"gpg.format", "ssh"},
		{"gpg.ssh.program", "/tmp/code-sign"},
		{"commit.gpgsign", "true"},
		{"http.proxyAuthMethod", "basic"},
	}

	// 0xb06e90-0xb071f0: Apply each config pair via git config --global
	for _, cfg := range configs {
		cmd := exec.CommandContext(ctx, "git", "config", "--global", cfg.key, cfg.value)
		output, err := cmd.CombinedOutput()
		if err != nil {
			// 0xb070ea: Log error at Warn
			e.logger.Warn("Failed to set git config",
				"key", cfg.key,
				"value", cfg.value,
				"error", err,
				"output", string(output),
			)
		} else {
			// 0xb071f0: Log success at Debug
			e.logger.Debug("Set git config",
				"key", cfg.key,
				"value", cfg.value,
			)
		}
	}

	// 0xb07218: Get HOME env var
	home := os.Getenv("HOME")

	// 0xb0725e: Create $HOME/.ssh directory
	sshDir := filepath.Join(home, ".ssh")
	// 0xb07305: MkdirAll with mode 0700 (0x1c0)
	if err := os.MkdirAll(sshDir, 0o700); err != nil {
		e.logger.Warn("Failed to create .ssh directory",
			"error", err,
		)
		return nil
	}

	// 0xb0736c: Build signing key file path
	signingKeyPath := filepath.Join(sshDir, "commit_signing_key.pub")

	// 0xb07459: Create empty signing key file (O_WRONLY|O_CREATE|O_TRUNC = 0x242, perm 0x1b6 = 0666)
	f, err := os.OpenFile(signingKeyPath, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o666)
	if err != nil {
		e.logger.Warn("Failed to create signing key file",
			"error", err,
		)
	} else {
		f.Close()
		// 0xb0751f: Log success
		e.logger.Debug("Created empty signing key file",
			"path", signingKeyPath,
		)
	}

	// 0xb075b2: Set git config for user.signingkey
	cmd := exec.CommandContext(ctx, "git", "config", "--global", "user.signingkey", signingKeyPath)
	output, err := cmd.CombinedOutput()
	if err != nil {
		// 0xb07739: Log error
		e.logger.Warn("Failed to set git signing key config",
			"key", "user.signingkey",
			"value", signingKeyPath,
			"error", err,
			"output", string(output),
		)
	} else {
		// 0xb077f1: Log success
		e.logger.Debug("Set git signing key",
			"key", "user.signingkey",
			"value", signingKeyPath,
		)
	}

	// 0xb07832: Log completion
	e.logger.Info("Git configuration setup completed")

	return nil
}

// bootstrapClaudeSettings sets up Claude Code settings and stop hook script.
// Creates ~/.claude directory, writes settings.json and stop-hook-git-check.sh
// if they don't already exist.
//
// Binary address: 0xb06560
// Source file: byoc.go
//
// Assembly flow:
//  1. 0xb065be: os.UserHomeDir(); error: "failed to get home directory: %w" (len=0x20)
//  2. filepath.Join(home, ".claude") (len=7 for ".claude")
//  3. filepath.Join(claudeDir, "settings.json") (len=0xd)
//  4. filepath.Join(claudeDir, "stop-hook-git-check.sh") (len=0x16)
//  5. 0xb06751: MkdirAll(claudeDir, 0x1ed=0755); error: "failed to create .claude directory: %w" (len=0x26)
//  6. 0xb06817: os.Stat(settingsPath); if exists: "Claude settings file already exists" (len=0x23)
//  7. 0xb068e9: os.Stat(stopHookPath); if exists: "Stop hook script already exists" (len=0x1f)
//  8. 0xb0697a: WriteFile(settingsPath, defaultSettingsJSON, 0x180=0600);
//     error: "failed to write settings file: %w" (len=0x21)
//  9. 0xb06a21: success: "Successfully created Claude settings file" (len=0x29)
//  10. 0xb06aab: WriteFile(stopHookPath, stopHookScript, 0x1ed=0755);
//     error: "failed to write stop hook script: %w" (len=0x24)
//  11. 0xb06b56: success: "Successfully created stop hook script" (len=0x25)
func (e *byocEnvironmentType) bootstrapClaudeSettings(ctx context.Context) error {
	// 0xb065be: Get home directory
	homeDir, err := os.UserHomeDir()
	if err != nil {
		return fmt.Errorf("failed to get home directory: %w", err)
	}

	// Build paths
	claudeDir := filepath.Join(homeDir, ".claude")
	settingsPath := filepath.Join(claudeDir, "settings.json")
	stopHookPath := filepath.Join(claudeDir, "stop-hook-git-check.sh")

	// 0xb06751: Create .claude directory
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		return fmt.Errorf("failed to create .claude directory: %w", err)
	}

	// 0xb06817: Check if settings file already exists
	settingsExists := false
	if _, err := os.Stat(settingsPath); err == nil {
		settingsExists = true
		e.logger.Info("Claude settings file already exists",
			"path", settingsPath,
		)
	}

	// 0xb068e9: Check if stop hook already exists
	stopHookExists := false
	if _, err := os.Stat(stopHookPath); err == nil {
		stopHookExists = true
		e.logger.Info("Stop hook script already exists",
			"path", stopHookPath,
		)
	}

	// 0xb0697a: Write settings file if it doesn't exist
	if !settingsExists {
		if err := os.WriteFile(settingsPath, defaultSettingsJSON, 0o600); err != nil {
			return fmt.Errorf("failed to write settings file: %w", err)
		}
		// 0xb06a21: Log success
		e.logger.Info("Successfully created Claude settings file",
			"path", settingsPath,
		)
	}

	// 0xb06aab: Write stop hook if it doesn't exist
	if !stopHookExists {
		if err := os.WriteFile(stopHookPath, stopHookScript, 0o755); err != nil {
			return fmt.Errorf("failed to write stop hook script: %w", err)
		}
		// 0xb06b56: Log success
		e.logger.Info("Successfully created stop hook script",
			"path", stopHookPath,
		)
	}

	return nil
}

// Ensure unused imports are referenced.
var _ = textproto.CanonicalMIMEHeaderKey
