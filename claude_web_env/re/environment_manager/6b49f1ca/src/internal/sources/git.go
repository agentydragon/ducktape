// Reconstructed from binary at /tmp/em-re/environment-manager
// Build ID: 6b49f1ca, Go 1.25.6
// Package: internal/sources
// Source: internal/sources/git.go

package sources

import (
	"context"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/auth"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/gitproxy"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y/diag"
)

// sizeResult holds the result of a pack size calculation.
//
// Struct layout (from type equality at 0xaf8400):
//
//	offset 0x00: size int64
//	offset 0x08: duration time.Duration
type sizeResult struct {
	size     int64
	duration time.Duration
}

// GitHandler handles git repository sources.
//
// Struct layout (from NewGitHandler field stores and Process field accesses):
//
//	offset 0x00: logger *slog.Logger
//	offset 0x08: baseDir string (ptr + len at 0x10)
//	offset 0x18: sessionID string (ptr + len at 0x20)
//	offset 0x28: gitProxyManager interface{} (itab + data at 0x30) — stored from NewGitHandler R8/R9
//	offset 0x38: authProvider auth.SourceAuthProvider (itab + data at 0x40) — set by createSourceAuthProvider in Process
//	offset 0x48: activityRecorder interface{} (itab + data at 0x50) — stored from NewGitHandler R10/R11
//	offset 0x58: processMode string (ptr + len at 0x60) — "fresh", "allow-prefetched", "resume", etc.
//	offset 0x68: gitProxyManager gitproxy.Manager (itab + data at 0x70) — used in setupLocalGitProxy; set after construction
//	offset 0x78: isResume bool
//	offset 0x80: postCloneHookPath string (ptr + len at 0x88) — from POST_CLONE_HOOK_PATH env var
//
// Implements: SourceHandler (itab at 0xf61188)
type GitHandler struct {
	logger           *slog.Logger            // offset 0x00
	baseDir          string                  // offset 0x08
	sessionID        string                  // offset 0x18
	gitProxyConfig   interface{}             // offset 0x28 — original proxy config interface from constructor
	authProvider     auth.SourceAuthProvider  // offset 0x38 — set per-source in Process via createSourceAuthProvider
	activityRecorder interface{}             // offset 0x48
	processMode      string                  // offset 0x58 — e.g. "fresh", "allow-prefetched", "resume"
	gitProxyManager  gitproxy.Manager        // offset 0x68 — concrete Manager interface, set post-construction
	isResume         bool                    // offset 0x78
	postCloneHookPath string                 // offset 0x80
}

// NewGitHandler creates a new GitHandler.
// It reads the POST_CLONE_HOOK_PATH environment variable and logs if present.
//
// Binary address: 0xaea460
// Source file: git.go
//
// Parameters (register ABI):
//
//	AX: logger, BX+CX: baseDir, DI+SI: sessionID,
//	R8+R9: gitProxyConfig (interface{}), R10+R11: activityRecorder (interface{}),
//	stack[0]+stack[1]: processMode string, stack[2]: isResume bool
func NewGitHandler(
	logger *slog.Logger,
	baseDir string,
	sessionID string,
	gitProxyConfig interface{},
	activityRecorder interface{},
	processMode string,
	isResume bool,
) *GitHandler {
	// Binary 0xaea4ba: os.Getenv("POST_CLONE_HOOK_PATH")
	postCloneHookPath := os.Getenv("POST_CLONE_HOOK_PATH")
	if postCloneHookPath != "" {
		// Binary 0xaea516-0xaea549: slog.Logger.log with context.Background()
		logger.Info("Using post-clone hook from environment",
			"path", postCloneHookPath,
		)
	}

	// Binary 0xaea54e: runtime.newobject allocates the GitHandler struct
	// Fields stored at 0xaea603-0xaea665
	return &GitHandler{
		logger:            logger,            // 0x00
		baseDir:           baseDir,           // 0x08
		sessionID:         sessionID,         // 0x18
		gitProxyConfig:    gitProxyConfig,    // 0x28
		activityRecorder:  activityRecorder,  // 0x48
		processMode:       processMode,       // 0x58
		isResume:          isResume,           // 0x78
		postCloneHookPath: postCloneHookPath, // 0x80
	}
}

// CanHandle returns true if the source type is "git_repository".
//
// Binary address: 0xaea6e0
// Source file: git.go
//
// Inline comparison: checks type string length == 14 and content == "git_repository"
func (h *GitHandler) CanHandle(source config.Source) bool {
	return source.GetType() == "git_repository"
}

// Process handles the full lifecycle of a git repository source:
// cloning, branch setup, and post-processing.
//
// Binary address: 0xaea720
// Source file: git.go
//
// Closure:
//
//	deferwrap1 at 0xaec7e0 — deferred o11y metric recording
//
// Key behaviors from disassembly:
//   - Type-asserts source to GitRepositorySource (0xaea790-0xaea7c2)
//   - Reads h.processMode (offset 0x58/0x60) to determine modeLabel:
//     "fresh" → "clone", otherwise → "fetch" (0xaea841-0xaea872)
//   - Constructs activityMsg = fmt.Sprintf("Starting repository %s", modeLabel) (0xaeab00)
//   - Logs with attrs: repo, provider (=GitInfo.Type), has_custom_url (=Ref!=nil), type (=processMode)
//   - Checks processMode for "allow-prefetched" (0xaeaa72) → skips URL construction
//   - If not allow-prefetched: checks processMode for "resume" → activityMsg = "Fetching repository %s"
//     else → activityMsg = "Cloning repository %s"
//   - URL construction based on source.GitInfo.Type:
//     "github" → "https://github.com/%s", "test-file" → "file://%s",
//     "github-ssh" → "git@github.com:%s.git", custom URL → source.GitInfo.URL
//   - Calls createSourceAuthProvider (0xaeb532), stores at h.authProvider (offset 0x38/0x40)
//   - Deferred o11y.RecordFunctionDeferred with GitCheckoutMetric (0xaeac05)
//   - Calls getAuthenticatedURL (0xaeb5b4) with permission "read"
//   - Calls config.GitRepositorySource.GetDirectory (0xaeaf53)
//   - Mode-dependent clone/fetch behavior
//   - Calls setupBranchFromOutcomes, resetOriginURL, runPostCloneHook
func (h *GitHandler) Process(ctx context.Context, logger *slog.Logger, source config.Source) error {
	// Binary 0xaea783: time.Now()
	startTime := time.Now()
	_ = startTime

	// Binary 0xaea790-0xaea7c2: type assertion to GitRepositorySource
	gitSource, ok := source.(config.GitRepositorySource)
	if !ok {
		return fmt.Errorf("source is not a GitRepositorySource")
	}

	// Binary 0xaea841-0xaea860: check h.processMode == "fresh" for modeLabel
	var modeLabel string
	if h.processMode == "fresh" {
		modeLabel = "clone"
	} else {
		modeLabel = "fetch"
	}

	// Binary 0xaea89f-0xaeab00: fmt.Sprintf("Starting repository %s", modeLabel)
	activityMsg := fmt.Sprintf("Starting repository %s", modeLabel)

	// Binary 0xaeaa65: logger.Info with repo, provider, has_custom_url, type attrs
	// Note: "provider" is source.GitInfo.Type, "has_custom_url" checks Ref!=nil,
	// "type" is h.processMode
	logger.Info(activityMsg,
		"repo", gitSource.GitInfo.Repo,
		"provider", gitSource.GitInfo.Type,
		"has_custom_url", gitSource.GitInfo.Ref != nil,
		"type", h.processMode,
	)

	// Binary 0xaeaa72-0xaeaaa9: if h.processMode == "allow-prefetched", skip URL construction
	// Binary 0xaeaaaa-0xaeab8d: else, construct "Cloning"/"Fetching" message based on mode
	if h.processMode != "allow-prefetched" {
		if h.processMode == "resume" {
			// Binary 0xaeab5f-0xaeab80: fmt.Sprintf("Fetching repository %s", repo)
			activityMsg = fmt.Sprintf("Fetching repository %s", gitSource.GitInfo.Repo)
		} else {
			// Binary 0xaeaadf-0xaeab00: fmt.Sprintf("Cloning repository %s", repo)
			activityMsg = fmt.Sprintf("Cloning repository %s", gitSource.GitInfo.Repo)
		}
	}

	// Binary 0xaeac2f-0xaead6c: construct repoURL
	// If source.GitInfo.URL != nil and *source.GitInfo.URL != "":
	//   use custom URL, log "Using custom git URL"
	// Else: switch on source.GitInfo.Type
	var repoURL string
	if gitSource.GitInfo.URL != nil && *gitSource.GitInfo.URL != "" {
		// Binary 0xaeac49-0xaead5c: custom URL path
		repoURL = *gitSource.GitInfo.URL
		logger.Info("Using custom git URL",
			"repo", gitSource.GitInfo.Repo,
			"url", repoURL,
		)
	} else {
		// Binary 0xaead71-0xaeaf06: switch on source.GitInfo.Type
		switch gitSource.GitInfo.Type {
		case "github":
			// Binary 0xaead9f-0xaeadf5: fmt.Sprintf("https://github.com/%s", repo)
			repoURL = fmt.Sprintf("https://github.com/%s", gitSource.GitInfo.Repo)
		case "test-file":
			// Binary 0xaeae26-0xaeae80: fmt.Sprintf("file://%s", repo)
			repoURL = fmt.Sprintf("file://%s", gitSource.GitInfo.Repo)
		case "github-ssh":
			// Binary 0xaeaeb5-0xaeaf06: fmt.Sprintf("git@github.com:%s.git", repo)
			repoURL = fmt.Sprintf("git@github.com:%s.git", gitSource.GitInfo.Repo)
		default:
			// Binary 0xaec665: error path for unsupported provider
			return fmt.Errorf("unsupported git provider type: %s", gitSource.GitInfo.Type)
		}
	}

	// Binary 0xaeaf0b-0xaeaf53: get repo directory
	// Loads h.baseDir, copies source to stack, calls GetDirectory
	repoDir := gitSource.GetDirectory()
	if repoDir == "" {
		// Binary 0xaec5c5: error when directory is empty
		return fmt.Errorf("could not determine repository directory for repo: %s", gitSource.GitInfo.Repo)
	}

	// Binary 0xaeaf79-0xaeaf9f: check h.processMode == "fresh"
	if h.processMode == "fresh" {
		// Binary 0xaeafa5-...: fresh mode — stat the directory, mkdir, clone
		// Ensure parent directory exists
		err := os.MkdirAll(filepath.Dir(repoDir), 0o755)
		if err != nil {
			return fmt.Errorf("failed to create base directory %s: %w", filepath.Dir(repoDir), err)
		}

		// Handle existing directory in fresh mode — remove it
		if _, statErr := os.Stat(repoDir); statErr == nil {
			logger.Info("Repository directory already exists, removing it")
			if err := os.RemoveAll(repoDir); err != nil {
				return fmt.Errorf("failed to remove existing directory: %w", err)
			}
		}
	}

	// Binary 0xaeb4be-0xaeb4e6: check source.GitInfo.Auth != nil && Auth.Token != ""
	// Binary 0xaeb532: createSourceAuthProvider(ctx, gitInfo) → stored at h.authProvider
	h.authProvider = h.createSourceAuthProvider(ctx, gitSource.GitInfo)

	// Binary 0xaeabee-0xaeac15: o11y.RecordFunctionDeferred(ctx, o11y.GitCheckoutMetric, ...)
	// AX=ctx.itab, BX=ctx.data, CX=GitCheckoutMetric, DI=resultObj, SI=nil, R8=nil, R9=nil
	deferredMetric := o11y.RecordFunctionDeferred(ctx, o11y.GitCheckoutMetric, nil, nil)
	defer deferredMetric()

	// Binary 0xaeb56e-0xaeb5b4: getAuthenticatedURL
	// Passes ctx, repoURL, authProvider, permission="read"
	authenticatedURL := h.getAuthenticatedURL(ctx, repoURL, h.authProvider, "read")

	// Binary 0xaeb5d1-0xaeb5e6: check h.isResume (offset 0x78) and h.processMode == "allow-prefetched"
	if h.isResume && h.processMode == "allow-prefetched" {
		// Binary 0xaeb616-...: allow-prefetched resume path
		if _, statErr := os.Stat(repoDir); statErr == nil {
			// Repository exists, use existing clone
			logger.Info("Repository already exists in allow-prefetched mode, using existing clone")
		}
	}

	// Binary: clone/fetch the repository
	cloneErr := h.cloneRepository(ctx, logger, gitSource, authenticatedURL, repoDir, repoURL, h.processMode)

	// Binary: setup branch from outcomes
	branchErr := h.setupBranchFromOutcomes(ctx, logger, gitSource, repoDir, authenticatedURL, h.authProvider)

	if cloneErr != nil && branchErr != nil {
		if h.isResume {
			return fmt.Errorf("failed to setup branch from outcomes (clone also failed: %w): %w", cloneErr, branchErr)
		}
		return fmt.Errorf("failed to clone repository: %w", cloneErr)
	}

	if cloneErr != nil && branchErr == nil {
		if h.isResume {
			logger.Warn("Repository branch setup succeeded but clone/fetch had errors",
				"error", cloneErr,
			)
		}
	}

	if branchErr != nil {
		return fmt.Errorf("failed to setup branch from outcomes: %w", branchErr)
	}

	// Binary: reset origin URL to non-authenticated
	h.resetOriginURL(ctx, logger, repoDir, repoURL)

	// Binary: run post-clone hook
	h.runPostCloneHook(ctx, logger, repoDir, gitSource.GitInfo.Repo)

	// Binary: final success log
	logger.Info("Git repository cloned successfully",
		"repo", gitSource.GitInfo.Repo,
		"duration_ms", time.Since(startTime).Milliseconds(),
	)

	_ = activityMsg
	return nil
}

// ValidateRepositoryAccess validates that the configured credentials
// can access the repository. It retries up to 3 times with exponential
// backoff, using context cancellation to interrupt waits.
//
// Binary address: 0xaed760
// Source file: git.go
//
// Key behaviors:
//   - Retries up to 3 times with exponential backoff
//   - Uses selectgo with timer and context.Done channel
//   - Logs "Validating %s repository access" with process_mode
//   - On success: logs "Repository access validated successfully"
//   - On failure: logs "repository_access_validation_failed" via diag
//   - Returns "repository access validation failed: %w" on error
func (h *GitHandler) ValidateRepositoryAccess(
	ctx context.Context,
	logger *slog.Logger,
	source config.Source,
	processMode string,
	repoDir string,
	authenticatedURL string,
) error {
	startTime := time.Now()

	logger.Info(fmt.Sprintf("Validating %s repository access", processMode),
		"repo", repoDir,
		"process_mode", processMode,
	)

	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 {
			// Exponential backoff
			backoff := time.Duration(1<<uint(attempt-1)) * time.Second
			logger.Info("Retrying git proxy validation",
				"attempt", attempt+1,
				"backoff_seconds", backoff.Seconds(),
			)

			timer := time.NewTimer(backoff)
			select {
			case <-ctx.Done():
				timer.Stop()
				return fmt.Errorf("context cancelled during validation retry backoff: %w", ctx.Err())
			case <-timer.C:
			}
		}

		// Run git ls-remote to validate access
		cmd := exec.CommandContext(ctx, "git", "ls-remote", "--heads", authenticatedURL)
		cmd.Dir = repoDir
		cmd.Env = os.Environ()

		_, err := cmd.CombinedOutput()
		if err == nil {
			if attempt > 0 {
				logger.Info("Git proxy validation succeeded after retry")
			} else {
				logger.Info("Git proxy validation succeeded")
			}
			logger.Info("Repository access validated successfully")
			return nil
		}

		lastErr = err
		if attempt < 2 {
			logger.Warn("Git proxy validation failed, will retry",
				"error", err,
				"attempt", attempt+1,
			)
		}
	}

	logger.Error("Git proxy validation failed after all retries",
		"error", lastErr,
		"attempts", 3,
	)

	// Binary: diag.LogEnvManagerNoPII(ctx, "repository_access_validation_failed", nil)
	diag.LogEnvManagerNoPII(ctx, "repository_access_validation_failed", nil)

	_ = startTime
	return fmt.Errorf("git proxy validation failed after %d attempts: %w", 3, lastErr)
}

// cloneRepository clones a git repository to the specified directory.
// It handles different modes: fresh clone, allow-prefetched, and resume.
//
// Binary address: 0xaef060
// Source file: git.go
//
// Closures:
//
//	func1 at 0xaf2200 — goroutine for computing pack size
//	func2 at 0xaf1fc0 — goroutine for logging
//	func3 at 0xaf1ee0 — goroutine for resetting origin URL
//
// Key behaviors:
//   - Logs "Cloning repository" with source details
//   - Records "git_clone_started" via diag
//   - In allow-prefetched mode with existing repo: logs and returns early
//   - Runs git clone with --no-checkout if needed
//   - Handles resume mode: fetches latest HEAD, continues with existing state
//   - Disables auto gc: "git config gc.auto 0"
//   - Handles empty repositories (no commits)
//   - Calculates pack size after clone
//   - Records "git_clone_completed" via diag with duration_ms, repo_size_bytes, pack_size_duration_ms
func (h *GitHandler) cloneRepository(
	ctx context.Context,
	logger *slog.Logger,
	source config.GitRepositorySource,
	authenticatedURL string,
	repoDir string,
	repoURL string,
	processMode string,
) error {
	isCustomURL := authenticatedURL != repoURL

	logger.Info("Cloning repository",
		"repo", source.GitInfo.Repo,
		"url", source.GitInfo.Repo,
		"is_custom_url", isCustomURL,
	)

	startTime := time.Now()

	// Binary: diag.LogEnvManagerNoPII(ctx, "git_clone_started", nil)
	diag.LogEnvManagerNoPII(ctx, "git_clone_started", nil)

	// Check for allow-prefetched mode with existing repo
	if processMode == "allow-prefetched" {
		if _, err := os.Stat(repoDir); err == nil {
			logger.Info("Repository already exists in allow-prefetched mode, using existing clone")
			// Disable auto gc
			_, gcErr := h.runGitCommand(ctx, logger, repoDir, "config", "gc.auto", "0")
			if gcErr != nil {
				logger.Warn("Failed to disable auto gc")
			}
			return nil
		}
		logger.Info("Repository directory does not exist in allow-prefetched mode, will fall back to fresh clone")
	}

	// Check for resume mode
	if h.isResume {
		if _, err := os.Stat(repoDir); err == nil {
			// Fetch latest HEAD for resume
			logger.Info("Fetching latest HEAD for resume")
			_, fetchErr := h.runGitCommand(ctx, logger, repoDir, "fetch", "origin")
			if fetchErr != nil {
				logger.Warn("Failed to fetch latest HEAD, continuing with existing state")
			} else {
				logger.Info("Successfully fetched latest HEAD")
			}
			return nil
		}
	}

	// Clone the repository
	if source.GitInfo.Ref != nil && *source.GitInfo.Ref != "" {
		// Clone with specific ref
		logger.Info("Fetching specific ref")
	}

	// Default clone
	logger.Info("Cloning default branch")

	args := []string{"clone"}
	if processMode != "test-file" {
		args = append(args, "--no-checkout")
	}
	args = append(args, authenticatedURL, repoDir)

	_, err := h.runGitCommand(ctx, logger, "", args[0], args[1:]...)
	if err != nil {
		logger.Error("Failed to clone repository",
			"error", err,
		)
		// Binary: diag.LogEnvManagerNoPII(ctx, "git_clone_failed", nil)
		diag.LogEnvManagerNoPII(ctx, "git_clone_failed", nil)
		return fmt.Errorf("failed to clone repository: %w", err)
	}

	// Check if repository is empty
	empty, _ := h.isEmptyRepository(ctx, logger, repoDir, authenticatedURL)
	if empty {
		logger.Info("Repository is empty (no commits), skipping checkout")
	}

	// Disable auto gc
	_, gcErr := h.runGitCommand(ctx, logger, repoDir, "config", "gc.auto", "0")
	if gcErr != nil {
		logger.Warn("Failed to disable auto gc")
	}

	// Calculate pack size
	packSizeStart := time.Now()
	repoSize, packErr := packSize(repoDir)
	packDuration := time.Since(packSizeStart)
	if packErr != nil {
		logger.Warn("Failed to calculate repo size",
			"error", packErr,
		)
	}

	elapsed := time.Since(startTime)
	logger.Info("Repository cloned successfully",
		"duration_ms", elapsed.Milliseconds(),
		"repo_size_bytes", repoSize,
		"pack_size_duration_ms", packDuration.Milliseconds(),
	)

	// Binary: diag.LogEnvManagerNoPII(ctx, "git_clone_completed", data)
	diag.LogEnvManagerNoPII(ctx, "git_clone_completed", map[string]interface{}{
		"duration_ms":           elapsed.Milliseconds(),
		"repo_size_bytes":       repoSize,
		"pack_size_duration_ms": packDuration.Milliseconds(),
	})

	// Reset origin URL to non-authenticated URL
	logger.Info("Resetting origin URL to non-authenticated URL")
	_, resetErr := h.runGitCommand(ctx, logger, repoDir, "remote", "set-url", "origin", repoURL)
	if resetErr != nil {
		logger.Warn("Failed to reset origin URL",
			"error", resetErr,
		)
	}

	return nil
}

// runGitFetchWithRetry runs a git fetch command with retry logic.
// It retries up to 4 times with exponential backoff based on a
// 5-second base interval, using selectgo for timer/context cancellation.
//
// Binary address: 0xaec9e0
// Source file: git.go
//
// Key behaviors:
//   - Retries up to 4 times (5 total attempts)
//   - Exponential backoff: base * 2^(attempt-1) seconds
//   - On retry: logs "Retrying git fetch after failure"
//   - May enable HTTP/1.1: logs "Using HTTP/1.1 for this attempt"
//   - May enable GIT_TRACE_PACKET: logs "Enabling GIT_TRACE_PACKET for debugging"
//   - On transient failure: logs "Git fetch failed, will retry"
//   - On retry success: logs "Git fetch succeeded after retry"
//   - On exhaustion: logs "Git fetch failed after all retries"
//   - Records "git_fetch_retry_exhausted" via diag on exhaustion
//   - Returns "context cancelled during retry backoff: %w" on ctx cancel
//   - Returns "git fetch failed after %d attempts: %w" on exhaustion
func (h *GitHandler) runGitFetchWithRetry(
	ctx context.Context,
	logger *slog.Logger,
	repoDir string,
	args ...string,
) (string, error) {
	startTime := time.Now()

	var lastErr error
	for attempt := 0; attempt <= 4; attempt++ {
		if attempt > 0 {
			// Exponential backoff
			backoff := time.Duration(1<<uint(attempt-1)) * 5 * time.Second

			logger.Warn("Retrying git fetch after failure",
				"error", lastErr,
				"attempt", attempt,
				"backoff_seconds", backoff.Seconds(),
			)

			// HTTP/1.1 fallback on later attempts
			if attempt >= 2 {
				logger.Info("Using HTTP/1.1 for this attempt")
			}

			// Enable GIT_TRACE_PACKET on later attempts
			if attempt >= 3 {
				logger.Info("Enabling GIT_TRACE_PACKET for debugging")
			}

			timer := time.NewTimer(backoff)
			select {
			case <-ctx.Done():
				timer.Stop()
				return "", fmt.Errorf("context cancelled during retry backoff: %w", ctx.Err())
			case <-timer.C:
			}
		}

		output, err := h.runGitCommand(ctx, logger, repoDir, args[0], args[1:]...)
		if err == nil {
			if attempt > 0 {
				logger.Info("Git fetch succeeded after retry",
					"attempts", attempt+1,
				)
			}
			return output, nil
		}

		lastErr = err
		if attempt < 4 {
			logger.Warn("Git fetch failed, will retry",
				"error", err,
				"attempt", attempt+1,
			)
		}
	}

	elapsed := time.Since(startTime)
	_ = elapsed

	logger.Error("Git fetch failed after all retries",
		"error", lastErr,
		"attempts", 5,
	)

	// Binary: diag.LogEnvManagerNoPII(ctx, "git_fetch_retry_exhausted", nil)
	diag.LogEnvManagerNoPII(ctx, "git_fetch_retry_exhausted", nil)

	return "", fmt.Errorf("git fetch failed after %d attempts: %w", 5, lastErr)
}

// runGitCommand executes a git command and returns its combined output.
// It prepends "GIT_TERMINAL_PROMPT=0" and "GIT_ASKPASS=" to the environment
// to prevent interactive prompts, and adds credential helper configuration.
//
// Binary address: 0xaee2a0
// Source file: git.go
//
// Key behaviors:
//   - Creates exec.CommandContext with "git" and provided args
//   - Sets Dir to the provided working directory
//   - Prepends env vars: GIT_TERMINAL_PROMPT=0, GIT_ASKPASS=
//   - Appends user-provided env vars
//   - Logs "Executing git command" with sanitized args
//   - Logs "Git command succeeded" on success
//   - Logs "Git command failed" on failure with sanitized output
//   - Returns "git %s failed: %w\nOutput: %s" on failure
func (h *GitHandler) runGitCommand(
	ctx context.Context,
	logger *slog.Logger,
	dir string,
	command string,
	args ...string,
) (string, error) {
	startTime := time.Now()

	// Build the full args list with env var settings prepended
	fullArgs := make([]string, 0, len(args)+2)
	fullArgs = append(fullArgs, "-c", "credential.helper=")
	fullArgs = append(fullArgs, args...)

	cmdArgs := append([]string{command}, fullArgs...)
	cmd := exec.CommandContext(ctx, "git", cmdArgs...)

	if dir != "" {
		cmd.Dir = dir
	}

	// Set environment
	env := os.Environ()
	env = append(env, "GIT_TERMINAL_PROMPT=0")
	env = append(env, "GIT_ASKPASS=")
	cmd.Env = env

	logger.Info("Executing git command",
		"command", fmt.Sprintf("git %s", command),
	)

	output, err := cmd.CombinedOutput()

	elapsed := time.Since(startTime)

	if err != nil {
		outputStr := string(output)
		sanitized := h.sanitizeURL(outputStr)

		logger.Error("Git command failed",
			"command", fmt.Sprintf("git %s", command),
			"error", err,
			"duration_ms", elapsed.Milliseconds(),
			"output", sanitized,
		)

		return "", fmt.Errorf("git %s failed: %w\nOutput: %s", command, err, sanitized)
	}

	logger.Info("Git command succeeded",
		"command", fmt.Sprintf("git %s", command),
		"duration_ms", elapsed.Milliseconds(),
	)

	return string(output), nil
}

// getAuthenticatedURL returns the authenticated URL for a git repository
// by delegating to the auth provider if available.
//
// Binary address: 0xaec900
// Source file: git.go
//
// Parameters (register ABI):
//
//	AX: self, BX+CX: ctx, DI+SI: repoURL string,
//	R8+R9: authProvider (SourceAuthProvider interface),
//	R10+R11: permission string
//
// Key behaviors from disassembly:
//   - 0xaec932: TESTQ R8,R8 — if authProvider itab is nil, return ""
//   - 0xaec937: MOVQ 0x18(AX),DX — loads h.sessionID; if nil, return ""
//   - 0xaec945: MOVQ 0x18(R8),R12 — loads AuthenticateURL method from itab
//   - 0xaec95b: CALL R12 — calls authProvider.AuthenticateURL(...)
//   - Returns (authenticatedURL string, applied bool)
func (h *GitHandler) getAuthenticatedURL(
	ctx context.Context,
	repoURL string,
	authProvider auth.SourceAuthProvider,
	permission auth.Permission,
) (string, bool) {
	// Binary 0xaec932-0xaec935: nil check on authProvider
	if authProvider == nil {
		return "", false
	}

	// Binary 0xaec937-0xaec943: nil check on h.sessionID
	if h.sessionID == "" {
		return "", false
	}

	// Binary 0xaec945-0xaec95b: call authProvider.AuthenticateURL
	// The AuthContext is nil here (DI=0 from XORL), but the method is called
	// with the permission string from the caller.
	authenticatedURL, applied := authProvider.AuthenticateURL(ctx, nil, repoURL, permission)
	return authenticatedURL, applied
}

// createSourceAuthProvider creates the appropriate auth provider based
// on the source provider type and auth configuration.
//
// Binary address: 0xaec840
// Source file: git.go
//
// Parameters (register ABI):
//
//	AX: self, BX+CX: ctx (unused), stack: GitInfo struct
//
// Key behaviors from disassembly:
//   - 0xaec852: loads source.Auth (*AuthConfig) from stack offset 0x58
//   - 0xaec857: if Auth is nil, return (nil, nil)
//   - 0xaec85c-0xaec877: check source.Type == "github" (6 chars)
//   - 0xaec879-0xaec89a: if github, check Auth.Type == "github_app" (10 chars)
//   - 0xaec89c-0xaec8ac: if github_app: call auth.NewGitHubSourceAuthProvider(h.logger, Auth.Token)
//   - 0xaec8b2-0xaec8c0: default: call auth.NewGitHubSourceAuthProvider(h.logger, Auth.Token)
//   - Both paths call the same function; the github_app check is for future differentiation
func (h *GitHandler) createSourceAuthProvider(ctx context.Context, gitInfo config.GitInfo) auth.SourceAuthProvider {
	if gitInfo.Auth == nil {
		return nil
	}

	// Both github+github_app and default paths call the same function
	if gitInfo.Type == "github" && gitInfo.Auth.Type == "github_app" {
		return auth.NewGitHubSourceAuthProvider(h.logger, gitInfo.Auth.Token)
	}

	return auth.NewGitHubSourceAuthProvider(h.logger, gitInfo.Auth.Token)
}

// sanitizeURL removes credentials from URLs.
// It handles two cases:
//  1. Parseable URLs: reconstructs as scheme://host/path?query#fragment
//     with credentials stripped (replaces userinfo with "<token>")
//  2. Non-parseable strings with "://": splits on "://" and removes
//     the token between "://" and "@"
//
// Binary address: 0xaf3000
// Source file: git.go
//
// Key behaviors:
//   - Parses URL; if parse succeeds and has userinfo, strips it with "://<token>@"
//   - If URL has query, appends "?" + query
//   - If URL has fragment, appends "#" + fragment
//   - If parse fails, checks for "://" and "@" in string
//   - Splits on "://", finds "@" in second part, replaces with "://<token>"
func (h *GitHandler) sanitizeURL(rawURL string) string {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		// Fallback: try to find :// and @ patterns
		if !strings.Contains(rawURL, "://") {
			return rawURL
		}
		idx := strings.Index(rawURL, "@")
		if idx < 0 {
			return rawURL
		}

		parts := strings.SplitN(rawURL, "://", 2)
		if len(parts) != 2 {
			return rawURL
		}

		atIdx := strings.Index(parts[1], "@")
		if atIdx <= 0 {
			return rawURL
		}

		return parts[0] + "://<token>" + parts[1][atIdx:]
	}

	if parsed.User != nil {
		result := parsed.Scheme + "://<token>@" + parsed.Host + parsed.Path
		if parsed.RawQuery != "" {
			result += "?" + parsed.RawQuery
		}
		if parsed.Fragment != "" {
			result += "#" + parsed.Fragment
		}
		return result
	}

	return parsed.String()
}

// branchExistsOnRemote checks whether a branch exists on the remote
// by running "git ls-remote --heads <url> <branch>".
//
// Binary address: 0xaf3200
// Source file: git.go
//
// Key behaviors:
//   - Logs "Checking if branch exists on remote" with branch name
//   - Runs: git ls-remote --heads <authenticatedURL> refs/heads/<branch>
//   - Sets Dir and Env on the command
//   - Returns true if output is non-empty, false otherwise
func (h *GitHandler) branchExistsOnRemote(
	ctx context.Context,
	logger *slog.Logger,
	repoDir string,
	authenticatedURL string,
	branch string,
) bool {
	logger.Debug("Checking if branch exists on remote",
		"branch", branch,
	)

	cmd := exec.CommandContext(ctx, "git", "ls-remote", "--heads", authenticatedURL, "refs/heads/"+branch)
	cmd.Dir = repoDir
	cmd.Env = os.Environ()

	output, err := cmd.CombinedOutput()
	if err != nil {
		return false
	}

	return len(strings.TrimSpace(string(output))) > 0
}

// checkoutBranch checks out a branch in a repository. If the branch exists
// on the remote, it fetches and checks out the remote branch. Otherwise,
// it creates a new local branch.
//
// Binary address: 0xaf34a0
// Source file: git.go
//
// Key behaviors:
//   - Calls branchExistsOnRemote to check if branch exists
//   - If exists: calls fetchAndCheckoutRemoteBranch
//   - If not: calls createLocalBranch
func (h *GitHandler) checkoutBranch(
	ctx context.Context,
	logger *slog.Logger,
	repoDir string,
	authenticatedURL string,
	branch string,
) error {
	if h.branchExistsOnRemote(ctx, logger, repoDir, authenticatedURL, branch) {
		return h.fetchAndCheckoutRemoteBranch(ctx, logger, repoDir, authenticatedURL, branch)
	}
	return h.createLocalBranch(ctx, logger, repoDir, branch)
}

// fetchAndCheckoutRemoteBranch fetches a branch from the remote and checks it out.
//
// Binary address: 0xaf3660
// Source file: git.go
//
// Closure:
//
//	func1 at 0xaf3fe0 — deferred cleanup
//
// Key behaviors:
//   - Logs "Branch exists on remote, fetching and checking out"
//   - Sets authenticated URL for fetch
//   - Runs: git fetch origin <branch>:<branch>
//   - Runs: git checkout <branch>
//   - On fetch failure: returns "failed to fetch branch %s: %w"
//   - On checkout failure: returns "failed to checkout branch %s: %w"
func (h *GitHandler) fetchAndCheckoutRemoteBranch(
	ctx context.Context,
	logger *slog.Logger,
	repoDir string,
	authenticatedURL string,
	branch string,
) error {
	logger.Info("Branch exists on remote, fetching and checking out",
		"branch", branch,
	)

	// Set authenticated URL for fetch
	_, err := h.runGitCommand(ctx, logger, repoDir, "remote", "set-url", "origin", authenticatedURL)
	if err != nil {
		logger.Warn("Failed to set authenticated URL for fetch",
			"error", err,
		)
	}

	// Fetch the branch
	refspec := fmt.Sprintf("%s:%s", branch, branch)
	_, err = h.runGitCommand(ctx, logger, repoDir, "fetch", "origin", refspec)
	if err != nil {
		return fmt.Errorf("failed to fetch branch %s: %w", branch, err)
	}

	// Checkout the branch
	_, err = h.runGitCommand(ctx, logger, repoDir, "checkout", branch)
	if err != nil {
		return fmt.Errorf("failed to checkout branch %s: %w", branch, err)
	}

	return nil
}

// createLocalBranch creates a new local branch from the current HEAD.
//
// Binary address: 0xaf41c0
// Source file: git.go
//
// Key behaviors:
//   - Logs "Branch doesn't exist on remote, creating new branch locally from current HEAD"
//   - Runs: git checkout -b <branch>
//   - On failure: logs "Failed to create new branch"
//   - On failure: returns "failed to create branch %s: %w"
//   - On success: sets upstream to origin/<branch>
//   - Logs "Successfully created new branch locally"
func (h *GitHandler) createLocalBranch(
	ctx context.Context,
	logger *slog.Logger,
	repoDir string,
	branch string,
) error {
	logger.Info("Branch doesn't exist on remote, creating new branch locally from current HEAD")

	_, err := h.runGitCommand(ctx, logger, repoDir, "checkout", "-b", branch)
	if err != nil {
		logger.Error("Failed to create new branch",
			"branch", branch,
			"error", err,
		)
		return fmt.Errorf("failed to create branch %s: %w", branch, err)
	}

	// Set upstream tracking
	h.runGitCommand(ctx, logger, repoDir, "branch", "--set-upstream-to", "origin", branch)

	logger.Info("Successfully created new branch locally",
		"branch", branch,
	)

	return nil
}

// isEmptyRepository checks if a repository is empty (has no commits)
// by running "git ls-remote" and checking the output.
//
// Binary address: 0xaf2b20
// Source file: git.go
//
// Key behaviors:
//   - Logs "Checking if repository is empty"
//   - Runs: git ls-remote --heads <authenticatedURL> refs/heads/*
//   - If output is non-empty: logs "Repository is not empty or ls-remote failed", returns false
//   - If output is empty: logs "Repository confirmed empty - no commits", returns true
func (h *GitHandler) isEmptyRepository(
	ctx context.Context,
	logger *slog.Logger,
	repoDir string,
	authenticatedURL string,
) (bool, error) {
	logger.Info("Checking if repository is empty",
		"repo", repoDir,
	)

	cmd := exec.CommandContext(ctx, "git", "ls-remote", "--heads", authenticatedURL, "refs/heads/*")
	cmd.Dir = repoDir
	cmd.Env = os.Environ()

	output, err := cmd.CombinedOutput()
	if err != nil || len(strings.TrimSpace(string(output))) > 0 {
		logger.Info("Repository is not empty or ls-remote failed")
		return false, err
	}

	logger.Info("Repository confirmed empty - no commits")
	return true, nil
}

// runPostCloneHook executes the post-clone hook if one is configured
// via the POST_CLONE_HOOK_PATH environment variable.
//
// Binary address: 0xaf22e0
// Source file: git.go
//
// Key behaviors:
//   - If postCloneHookPath is empty (offset 0x88 == 0), returns immediately
//   - Logs "Running post-clone hook" with hook path and repo
//   - Runs: bash <hookPath>
//   - Writes combined output to file (0o644 permissions)
//   - On write failure: logs "Failed to write post-clone hook output to file"
//   - On hook failure: logs "Post-clone hook failed, continuing anyway"
//   - On success: logs "Post-clone hook completed successfully"
func (h *GitHandler) runPostCloneHook(
	ctx context.Context,
	logger *slog.Logger,
	repoDir string,
	repo string,
) {
	if h.postCloneHookPath == "" {
		return
	}

	logger.Info("Running post-clone hook",
		"hook_path", h.postCloneHookPath,
		"repo", repo,
	)

	cmd := exec.CommandContext(ctx, "bash", h.postCloneHookPath)
	cmd.Dir = repoDir
	cmd.Env = os.Environ()

	output, err := cmd.CombinedOutput()

	// Write output to file
	outputPath := filepath.Join(repoDir, ".post-clone-hook-output")
	writeErr := os.WriteFile(outputPath, output, 0o644)
	if writeErr != nil {
		logger.Error("Failed to write post-clone hook output to file",
			"error", writeErr,
		)
	}

	if err != nil {
		logger.Warn("Post-clone hook failed, continuing anyway",
			"error", err,
		)
		return
	}

	logger.Info("Post-clone hook completed successfully")
}

// setupBranchFromOutcomes sets up the branch for the repository
// based on the outcomes configuration.
//
// Binary address: 0xaf4740
// Source file: git.go
//
// Key behaviors:
//   - Checks if outcomes map has an entry for the repo
//   - Logs "Processing branch from outcomes"
//   - In resume mode: may create local branch directly
//     logs "Resume mode: creating local branch directly"
//     returns "failed to create local branch %s: %w" on failure
//   - Calls checkoutBranch for the target branch
//   - May skip if already on target branch:
//     logs "Already on target branch, skipping checkout"
//   - On success: logs "Successfully processed branch"
func (h *GitHandler) setupBranchFromOutcomes(
	ctx context.Context,
	logger *slog.Logger,
	source config.GitRepositorySource,
	repoDir string,
	authenticatedURL string,
	authProvider auth.SourceAuthProvider,
) error {
	// Binary: outcomes is at offset 0x68 (nil initially, set externally)
	// NOTE: The outcomes field is not currently in the struct at a known offset.
	// Based on binary analysis, it may be passed via a different mechanism.
	// For now, we use the authProvider for branch setup decisions.

	// The actual branch info comes from the source's Ref field
	if source.GitInfo.Ref == nil || *source.GitInfo.Ref == "" {
		return nil
	}

	branch := *source.GitInfo.Ref

	logger.Info("Processing branch from outcomes",
		"branch", branch,
		"repo", source.GitInfo.Repo,
	)

	// Check if resume mode with BYOC
	if h.isResume {
		// Check if task branch exists on remote
		logger.Info("BYOC resume: checking if task branch exists on remote")
		if h.branchExistsOnRemote(ctx, logger, repoDir, authenticatedURL, branch) {
			logger.Info("BYOC resume: task branch exists on remote, fetching it")
			err := h.fetchAndCheckoutRemoteBranch(ctx, logger, repoDir, authenticatedURL, branch)
			if err != nil {
				return fmt.Errorf("failed to fetch/checkout task branch: %w", err)
			}
			logger.Info("Repository resumed successfully with task branch")
			return nil
		}

		logger.Info("BYOC resume: task branch not found on remote, falling back to original ref")

		// Create local branch directly
		logger.Info("Resume mode: creating local branch directly")
		_, err := h.runGitCommand(ctx, logger, repoDir, "checkout", "-b", branch)
		if err != nil {
			return fmt.Errorf("failed to create local branch %s: %w", branch, err)
		}
	} else {
		// Normal mode: checkout or create branch
		err := h.checkoutBranch(ctx, logger, repoDir, authenticatedURL, branch)
		if err != nil {
			return err
		}
	}

	logger.Info("Successfully processed branch",
		"branch", branch,
	)

	return nil
}

// resetOriginURL resets the origin URL to the non-authenticated URL.
//
// Not a standalone binary function; called inline from Process/cloneRepository.
func (h *GitHandler) resetOriginURL(
	ctx context.Context,
	logger *slog.Logger,
	repoDir string,
	repoURL string,
) {
	logger.Info("Resetting origin URL to non-authenticated URL")
	_, err := h.runGitCommand(ctx, logger, repoDir, "remote", "set-url", "origin", repoURL)
	if err != nil {
		logger.Warn("Failed to reset origin URL",
			"error", err,
		)
	}
}

// UpdateRemoteURL updates the remote URL for a git repository source.
// It handles custom URLs and checks if the repository directory exists.
//
// Binary address: 0xaf4f80
// Source file: git.go
//
// Key behaviors:
//   - Type-asserts source to GitRepositorySource
//   - If source has no custom URL: logs "No custom URL specified, skipping remote URL update"
//   - Gets directory from source
//   - If directory doesn't exist: logs "Repository directory does not exist, skipping remote URL update"
//   - Logs "Updating to custom git URL"
//   - Runs: git remote set-url origin <customURL>
//   - On failure: logs "Failed to update remote URL"
//   - On failure: returns "failed to update remote URL for %s: %w"
//   - On success: logs "Successfully updated git remote URL"
func (h *GitHandler) UpdateRemoteURL(
	ctx context.Context,
	logger *slog.Logger,
	source config.Source,
) error {
	gitSource, ok := source.(config.GitRepositorySource)
	if !ok {
		return fmt.Errorf("unsupported git provider type: %s", source.GetType())
	}

	if gitSource.GitInfo.URL == nil || *gitSource.GitInfo.URL == "" {
		logger.Info("No custom URL specified, skipping remote URL update")
		return nil
	}

	repoDir := gitSource.GetDirectory()
	if repoDir == "" {
		return nil
	}

	if _, err := os.Stat(repoDir); err != nil {
		logger.Info("Repository directory does not exist, skipping remote URL update",
			"repo", gitSource.GitInfo.Repo,
			"directory", repoDir,
		)
		return nil
	}

	customURL := *gitSource.GitInfo.URL
	logger.Info("Updating to custom git URL",
		"repo", gitSource.GitInfo.Repo,
		"url", customURL,
	)

	_, err := h.runGitCommand(ctx, logger, repoDir, "remote", "set-url", "origin", customURL)
	if err != nil {
		logger.Error("Failed to update remote URL",
			"repo", gitSource.GitInfo.Repo,
			"error", err,
		)
		return fmt.Errorf("failed to update remote URL for %s: %w", gitSource.GitInfo.Repo, err)
	}

	logger.Info("Successfully updated git remote URL",
		"repo", gitSource.GitInfo.Repo,
	)

	return nil
}

// SetupGitProxyAfterSourcesProcessed sets up the local git proxy for
// all processed repositories. It configures git to use the proxy URL
// for authenticated operations.
//
// Binary address: 0xaf5920
// Source file: git.go
//
// Key behaviors from disassembly:
//   - Iterates over sources, filtering for "git_repository" type (0xaf5a1c-0xaf5a3a)
//   - Casts to GitRepositorySource to get directory
//   - Checks gitProxyManager (offset 0x68) for nil: loads itab, calls IsRunning at itab+0x20
//   - Gets proxy URL from gitProxyManager.GetProxyURL at itab+0x18
//   - Runs: git remote set-url origin <proxyURL>
//   - On failure: logs "failed to update git remote to use proxy: %w, output: %s"
//   - On success: logs "Git remote updated to use local proxy"
//   - Logs "Local git proxy started for all repositories" when complete
func (h *GitHandler) SetupGitProxyAfterSourcesProcessed(
	ctx context.Context,
	logger *slog.Logger,
	sources []config.Source,
) error {
	// Binary 0xaf6475: loads h.gitProxyManager (offset 0x68)
	// Binary 0xaf6479: TESTQ — nil check
	if h.gitProxyManager == nil {
		return fmt.Errorf("git proxy is not running")
	}

	// Binary 0xaf64aa: calls IsRunning via itab offset 0x20
	if !h.gitProxyManager.IsRunning() {
		return fmt.Errorf("git proxy is not running")
	}

	// Iterate sources looking for "git_repository" type
	for _, source := range sources {
		if source.GetType() != "git_repository" {
			continue
		}

		gitSource, ok := source.(config.GitRepositorySource)
		if !ok {
			continue
		}

		repoDir := gitSource.GetDirectory()
		if repoDir == "" {
			logger.Warn("Could not determine repo path for proxy setup",
				"repo", gitSource.GitInfo.Repo,
			)
			continue
		}

		err := h.setupLocalGitProxy(ctx, logger, repoDir, gitSource.GitInfo.Repo)
		if err != nil {
			logger.Error("Failed to setup local git proxy for repo",
				"repo", gitSource.GitInfo.Repo,
				"error", err,
			)
			continue
		}
	}

	logger.Info("Local git proxy started for all repositories")

	return nil
}

// setupLocalGitProxy configures a single repository to use the local
// git proxy by updating the remote URL.
//
// Binary address: 0xaf6440
// Source file: git.go
//
// Key behaviors from disassembly:
//   - 0xaf6475: loads h.gitProxyManager (offset 0x68), nil check
//   - 0xaf64aa: calls IsRunning via itab+0x20
//   - 0xaf64e5: calls GetProxyURL via itab+0x18
//   - Runs: git remote set-url origin <proxyURL>
//   - On failure: returns "failed to update git remote to use proxy: %w, output: %s"
//   - On success: logs "Git remote updated to use local proxy"
func (h *GitHandler) setupLocalGitProxy(
	ctx context.Context,
	logger *slog.Logger,
	repoDir string,
	repo string,
) error {
	// Binary 0xaf6475-0xaf64b4: nil check + IsRunning check
	if h.gitProxyManager == nil || !h.gitProxyManager.IsRunning() {
		return fmt.Errorf("git proxy is not running")
	}

	// Binary 0xaf64e5: calls GetProxyURL
	proxyURL, err := h.gitProxyManager.GetProxyURL(ctx, logger)
	if err != nil {
		return fmt.Errorf("failed to start git proxy: %w", err)
	}

	// Run git remote set-url with proxy URL
	cmd := exec.CommandContext(ctx, "git", "remote", "set-url", "origin", proxyURL)
	cmd.Dir = repoDir
	cmd.Env = os.Environ()

	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("failed to update git remote to use proxy: %w, output: %s", err, string(output))
	}

	logger.Info("Git remote updated to use local proxy",
		"repo", repo,
	)

	return nil
}

// packSize calculates the total size of pack files in a git repository.
//
// Binary address: 0xaf2880
// Source file: git.go
//
// Key behaviors:
//   - Builds path: repoDir + ".git" + "objects" + "pack"
//   - Reads the pack directory
//   - Filters for files with ".pack" extension
//   - Stats each .pack file and sums sizes
//   - Returns "failed to read pack directory: %w" on readdir failure
//   - Returns "failed to stat pack file %s: %w" on stat failure
func packSize(repoDir string) (int64, error) {
	packDir := filepath.Join(repoDir, ".git", "objects", "pack")

	entries, err := os.ReadDir(packDir)
	if err != nil {
		return 0, fmt.Errorf("failed to read pack directory: %w", err)
	}

	var totalSize int64
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}

		name := entry.Name()
		if !strings.HasSuffix(name, ".pack") {
			continue
		}

		info, err := entry.Info()
		if err != nil {
			return 0, fmt.Errorf("failed to stat pack file %s: %w", name, err)
		}

		totalSize += info.Size()
	}

	return totalSize, nil
}
