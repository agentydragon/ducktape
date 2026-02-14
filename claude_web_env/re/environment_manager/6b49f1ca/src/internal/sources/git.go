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
//   offset 0x00: size int64
//   offset 0x08: duration time.Duration
type sizeResult struct {
	size     int64
	duration time.Duration
}

// GitHandler handles git repository sources.
//
// Struct layout (from NewGitHandler field stores and method field accesses):
//   offset 0x00: logger *slog.Logger
//   offset 0x08: baseDir string (ptr)
//   offset 0x10: baseDir string (len)
//   offset 0x18: sessionID string (ptr)
//   offset 0x20: sessionID string (len)
//   offset 0x28: activityRecorder interface{} (itab)
//   offset 0x30: activityRecorder interface{} (data)
//   offset 0x38: unknown1 (from R10)
//   offset 0x48: unknown2 (from R10 -> 0x48)
//   offset 0x50: unknown3 (from R8 -> 0x50)
//   offset 0x58: gitProxyManager interface{} (itab)
//   offset 0x60: gitProxyManager interface{} (data)
//   offset 0x68: gitProxyManager gitproxy.Manager (itab, accessed in setupLocalGitProxy)
//   offset 0x70: gitProxyManager gitproxy.Manager (data)
//   offset 0x78: isResume bool
//   offset 0x80: postCloneHookPath string (ptr)
//   offset 0x88: postCloneHookPath string (len)
//
// Implements: SourceHandler (itab at 0xf61188)
type GitHandler struct {
	logger            *slog.Logger        // offset 0x00
	baseDir           string              // offset 0x08
	sessionID         string              // offset 0x18
	activityRecorder  interface{}         // offset 0x28
	outcomes          map[string][]string // offset 0x30 (branch outcomes map)
	repoAuths         interface{}         // offset 0x38
	authProvider      interface{}         // offset 0x48
	gitProxyConfig    interface{}         // offset 0x58
	gitProxyManager   gitproxy.Manager    // offset 0x68
	isResume          bool                // offset 0x78
	postCloneHookPath string              // offset 0x80
}

// NewGitHandler creates a new GitHandler.
// It reads the POST_CLONE_HOOK_PATH environment variable and logs if present.
//
// Binary address: 0xaea460
// Source file: git.go
func NewGitHandler(
	logger *slog.Logger,
	baseDir string,
	sessionID string,
	gitProxyManager interface{},
	activityRecorder interface{},
	isResume bool,
) *GitHandler {
	postCloneHookPath := os.Getenv("POST_CLONE_HOOK_PATH")
	if postCloneHookPath != "" {
		logger.Info("Using post-clone hook from environment",
			"path", postCloneHookPath,
		)
	}

	return &GitHandler{
		logger:            logger,
		baseDir:           baseDir,
		sessionID:         sessionID,
		activityRecorder:  activityRecorder,
		gitProxyManager:   nil, // set separately
		isResume:          isResume,
		postCloneHookPath: postCloneHookPath,
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
//   deferwrap1 at 0xaec7e0 - deferred o11y metric recording
//
// Key behaviors:
//   - Casts source to GitRepositorySource
//   - Determines process_mode from source type ("fresh", "allow-prefetched", "resume", "test-file")
//   - Constructs git URL based on provider ("github" -> "https://github.com/%s",
//     "test-file" -> "file://%s", default -> "git@github.com:%s.git")
//   - Uses custom git URL if source has one configured
//   - Calls createSourceAuthProvider for authentication
//   - Records o11y.GitCheckoutMetric
//   - Calls cloneRepository for cloning
//   - Calls setupBranchFromOutcomes for branch setup
//   - Handles resume mode with fallback to branch setup on clone failure
func (h *GitHandler) Process(ctx context.Context, logger *slog.Logger, source config.Source) error {
	startTime := time.Now()

	// Type-assert to GitRepositorySource
	gitSource, ok := source.(config.GitRepositorySource)
	if !ok {
		return fmt.Errorf("source is not a GitRepositorySource")
	}

	// Determine process mode
	processMode := gitSource.GetType()
	var modeLabel string
	if processMode == "fresh" {
		modeLabel = "clone"
	} else {
		modeLabel = "clone"
	}

	activityMsg := fmt.Sprintf("Starting repository %s", gitSource.Repo)

	logger.Info(activityMsg,
		"repo", gitSource.Repo,
		"provider", gitSource.Provider,
		"has_custom_url", gitSource.CustomURL != "",
		"type", gitSource.GetType(),
	)

	// Check for "allow-prefetched" mode
	if processMode == "allow-prefetched" {
		// Use existing clone if available
	}

	// Construct the repository URL
	var repoURL string
	if gitSource.CustomURL != "" {
		repoURL = fmt.Sprintf("Cloning repository %s", gitSource.Repo)
		logger.Info("Using custom git URL",
			"repo", gitSource.Repo,
			"url", gitSource.Repo,
		)
	} else {
		switch gitSource.Provider {
		case "github":
			repoURL = fmt.Sprintf("https://github.com/%s", gitSource.Repo)
		case "test-file":
			repoURL = fmt.Sprintf("file://%s", gitSource.Repo)
		default:
			repoURL = fmt.Sprintf("git@github.com:%s.git", gitSource.Repo)
		}
	}

	activityMsg = fmt.Sprintf("Fetching repository %s", gitSource.Repo)

	// Create auth provider
	authProvider := h.createSourceAuthProvider(gitSource)

	// Record o11y metric
	deferredMetric := o11y.RecordFunctionDeferred(logger, ctx, o11y.GitCheckoutMetric, nil, nil)
	defer deferredMetric()

	// Get the authenticated URL
	authenticatedURL := h.getAuthenticatedURL(gitSource, repoURL, authProvider)

	// Get directory for the repo
	repoDir := gitSource.GetDirectory()
	if repoDir == "" {
		return fmt.Errorf("could not determine repository directory for repo: %s", gitSource.Repo)
	}

	// Ensure base directory exists
	err := os.MkdirAll(filepath.Dir(repoDir), 0o755)
	if err != nil {
		return fmt.Errorf("failed to create base directory %s: %w", filepath.Dir(repoDir), err)
	}

	// Handle existing directory
	if _, statErr := os.Stat(repoDir); statErr == nil {
		if processMode == "allow-prefetched" {
			// Use existing repo
		} else if h.isResume {
			// Resume mode
		} else {
			logger.Info("Repository directory already exists, removing it")
			if err := os.RemoveAll(repoDir); err != nil {
				return fmt.Errorf("failed to remove existing directory: %w", err)
			}
		}
	} else if h.isResume {
		logger.Warn(fmt.Sprintf("repository directory does not exist in resume mode: %s", repoDir))
	}

	// Clone the repository
	cloneErr := h.cloneRepository(ctx, logger, gitSource, authenticatedURL, repoDir, repoURL, modeLabel)

	// Setup branch from outcomes
	branchErr := h.setupBranchFromOutcomes(ctx, logger, gitSource, repoDir, authenticatedURL, authProvider)

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
			return fmt.Errorf("clone/fetch failed in resume mode: %w", cloneErr)
		}
	}

	if branchErr != nil {
		return fmt.Errorf("failed to setup branch from outcomes: %w", branchErr)
	}

	// Reset origin URL to non-authenticated
	h.resetOriginURL(ctx, logger, repoDir, repoURL)

	// Run post-clone hook
	h.runPostCloneHook(ctx, logger, repoDir, gitSource.Repo)

	logger.Info("Git repository cloned successfully",
		"repo", gitSource.Repo,
		"duration_ms", time.Since(startTime).Milliseconds(),
	)

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

	diag.LogEnvManagerNoPII(logger, ctx, "repository_access_validation_failed", nil)

	return fmt.Errorf("git proxy validation failed after %d attempts: %w", 3, lastErr)
}

// cloneRepository clones a git repository to the specified directory.
// It handles different modes: fresh clone, allow-prefetched, and resume.
//
// Binary address: 0xaef060
// Source file: git.go
//
// Closures:
//   func1 at 0xaf2200 - goroutine for computing pack size
//   func2 at 0xaf1fc0 - goroutine for logging
//   func3 at 0xaf1ee0 - goroutine for resetting origin URL
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
		"repo", source.Repo,
		"url", source.Repo,
		"is_custom_url", isCustomURL,
	)

	startTime := time.Now()

	diag.LogEnvManagerNoPII(logger, ctx, "git_clone_started", nil)

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
	if source.Ref != "" {
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
		diag.LogEnvManagerNoPII(logger, ctx, "git_clone_failed", nil)
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

	diag.LogEnvManagerNoPII(logger, ctx, "git_clone_completed", map[string]interface{}{
		"duration_ms":          elapsed.Milliseconds(),
		"repo_size_bytes":      repoSize,
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

	diag.LogEnvManagerNoPII(logger, ctx, "git_fetch_retry_exhausted", nil)

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
// Key behaviors:
//   - If authProvider is nil, returns empty string
//   - If source has no auth token (offset 0x18 is nil), returns empty string
//   - Otherwise calls authProvider.GetAuthenticatedURL()
func (h *GitHandler) getAuthenticatedURL(
	source config.GitRepositorySource,
	repoURL string,
	authProvider auth.SourceAuthProvider,
) string {
	if authProvider == nil {
		return ""
	}

	authenticatedURL, _ := authProvider.GetAuthenticatedURL(repoURL)
	return authenticatedURL
}

// createSourceAuthProvider creates the appropriate auth provider based
// on the source provider type.
//
// Binary address: 0xaec840
// Source file: git.go
//
// Key behaviors:
//   - If source has no auth (offset 0x58 is nil), returns nil
//   - For "github" provider with "github_app" auth type (10 chars):
//     calls auth.NewGitHubSourceAuthProvider with logger and app token
//   - For other providers: calls auth.NewGitHubSourceAuthProvider with
//     logger and token (same function, different path)
func (h *GitHandler) createSourceAuthProvider(source config.GitRepositorySource) auth.SourceAuthProvider {
	if source.Auth == nil {
		return nil
	}

	if source.Provider == "github" && source.Auth.Type == "github_app" {
		return auth.NewGitHubSourceAuthProvider(h.logger, source.Auth.Token)
	}

	return auth.NewGitHubSourceAuthProvider(h.logger, source.Auth.Token)
}

// sanitizeURL removes credentials from URLs.
// It handles two cases:
//   1. Parseable URLs: reconstructs as scheme://host/path?query#fragment
//      with credentials stripped (replaces userinfo with "<token>")
//   2. Non-parseable strings with "://": splits on "://" and removes
//      the token between "://" and "@"
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
//   func1 at 0xaf3fe0 - deferred cleanup
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
//   - Checks if outcomes map (offset 0x30) has an entry for the repo
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
	if h.outcomes == nil {
		return nil
	}

	branches, ok := h.outcomes[source.Repo]
	if !ok || len(branches) == 0 {
		return nil
	}

	branch := branches[0]

	logger.Info("Processing branch from outcomes",
		"branch", branch,
		"repo", source.Repo,
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

	if gitSource.CustomURL == "" {
		logger.Info("No custom URL specified, skipping remote URL update")
		return nil
	}

	repoDir := gitSource.GetDirectory()
	if repoDir == "" {
		return nil
	}

	if _, err := os.Stat(repoDir); err != nil {
		logger.Info("Repository directory does not exist, skipping remote URL update",
			"repo", gitSource.Repo,
			"directory", repoDir,
		)
		return nil
	}

	logger.Info("Updating to custom git URL",
		"repo", gitSource.Repo,
		"url", gitSource.CustomURL,
	)

	_, err := h.runGitCommand(ctx, logger, repoDir, "remote", "set-url", "origin", gitSource.CustomURL)
	if err != nil {
		logger.Error("Failed to update remote URL",
			"repo", gitSource.Repo,
			"error", err,
		)
		return fmt.Errorf("failed to update remote URL for %s: %w", gitSource.Repo, err)
	}

	logger.Info("Successfully updated git remote URL",
		"repo", gitSource.Repo,
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
// Key behaviors:
//   - Iterates over sources, filtering for "git_repository" type
//   - Casts to GitRepositorySource to get directory
//   - If gitProxyManager is nil or not running: returns "git proxy is not running"
//   - Gets proxy URL from gitProxyManager
//   - Runs: git remote set-url origin <proxyURL>
//   - On failure: logs "failed to update git remote to use proxy: %w, output: %s"
//   - On success: logs "Git remote updated to use local proxy"
//   - Logs "Local git proxy started for all repositories" when complete
func (h *GitHandler) SetupGitProxyAfterSourcesProcessed(
	ctx context.Context,
	logger *slog.Logger,
	sources []config.Source,
) error {
	if h.gitProxyManager == nil {
		return fmt.Errorf("git proxy is not running")
	}

	if !h.gitProxyManager.IsRunning() {
		return fmt.Errorf("git proxy is not running")
	}

	if h.sessionID == "" {
		return fmt.Errorf("session ID not available")
	}

	// Check if there are any git repositories
	hasGitRepos := false
	for _, source := range sources {
		if source.GetType() == "git_repository" {
			hasGitRepos = true
			break
		}
	}

	if !hasGitRepos {
		logger.Info("No repositories configured for git proxy")
		return nil
	}

	// Setup proxy for each git repository
	for _, source := range sources {
		if source.GetType() != "git_repository" {
			continue
		}

		gitSource, ok := source.(config.GitRepositorySource)
		if !ok {
			continue
		}

		if gitSource.Auth == nil {
			logger.Info("Skipping repo without auth",
				"repo", gitSource.Repo,
			)
			continue
		}

		repoDir := gitSource.GetDirectory()
		if repoDir == "" {
			logger.Warn("Could not determine repo path for proxy setup",
				"repo", gitSource.Repo,
			)
			continue
		}

		err := h.setupLocalGitProxy(ctx, logger, repoDir, gitSource.Repo)
		if err != nil {
			logger.Error("Failed to setup local git proxy for repo",
				"repo", gitSource.Repo,
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
// Key behaviors:
//   - Checks if gitProxyManager is nil/not running: returns "git proxy is not running"
//   - Gets proxy URL from gitProxyManager.GetProxyURL()
//   - Runs: git remote set-url origin <proxyURL>
//   - On failure: returns "failed to update git remote to use proxy: %w, output: %s"
//   - On success: logs "Git remote updated to use local proxy"
func (h *GitHandler) setupLocalGitProxy(
	ctx context.Context,
	logger *slog.Logger,
	repoDir string,
	repo string,
) error {
	if h.gitProxyManager == nil || !h.gitProxyManager.IsRunning() {
		return fmt.Errorf("git proxy is not running")
	}

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
