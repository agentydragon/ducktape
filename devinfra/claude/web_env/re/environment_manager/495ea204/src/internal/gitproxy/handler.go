// Reconstructed from a6f96673 DWARF extraction, carried forward to 495ea204.
// Package: internal/gitproxy
// Source: internal/gitproxy/handler.go

package gitproxy

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
)

// handler implements http.Handler for the git proxy.
//
// Struct layout (from type equality at 0xaea3a0 cross-referenced with field access):
//
//	offset 0x00: server *server (pointer to parent server config)
//	offset 0x08: httpClient *http.Client
//	offset 0x10: logger *slog.Logger
//
// Additional fields accessed via the server pointer:
//
//	server.repoAuths []RepoAuth (at server offset 0x20)
//	server.sessionIngressURL string (at server offset 0x10)
//	server.sessionID string (at server offset 0x00)
//
// Implements: net/http.Handler (itab at 0xf5cc00)
type handler struct {
	server     *server      // offset 0x00
	httpClient *http.Client // offset 0x08
	logger     *slog.Logger // offset 0x10
}

// validGitPaths contains the set of recognized git HTTP protocol path suffixes.
// Used by isValidGitPath to validate incoming requests.
var validGitPaths = []string{
	"info/refs",        // 9 chars
	"git-upload-pack",  // 15 chars
	"git-receive-pack", // 16 chars
	"HEAD",             // 4 chars
	"objects/",         // 8 chars (prefix match)
	"refs/",            // 5 chars (prefix match)
}

// isValidGitPath checks whether the given path is a valid git HTTP protocol path.
// It first strips any ".." components (path traversal prevention), then checks
// if the path starts with "/" (reject absolute paths), and finally checks against
// the known valid git path suffixes.
//
// Binary address: 0xae64a0
// Source file: handler.go
func isValidGitPath(path string) bool {
	// Check for ".." path traversal
	if strings.Contains(path, "..") {
		return false
	}

	if len(path) == 0 {
		return false
	}

	// Reject paths starting with "/"
	if path[0] == '/' {
		return false
	}

	// Check against known valid git paths
	for _, validPath := range validGitPaths {
		if len(path) >= len(validPath) && path[:len(validPath)] == validPath {
			return true
		}
	}

	return false
}

// sanitizeError extracts the error message string from an error interface
// and sanitizes it to remove credentials.
//
// Binary address: 0xae6180
// Source file: handler.go
func sanitizeError(err error) string {
	if err == nil {
		return ""
	}
	return sanitizeString(err.Error())
}

// sanitizeString removes sensitive credentials from a string.
// It replaces:
//   - Bearer tokens in Authorization headers
//   - Basic auth credentials in Authorization headers
//   - sk-ant-ccsr-* code signing tokens
//   - sk-ant-* API tokens
//
// Binary address: 0xae61e0
// Source file: handler.go (line 129+)
func sanitizeString(s string) string {
	// Phase 1: Replace authorization header tokens (4 patterns, case-sensitive pairs)
	tokenPatterns := []*regexp.Regexp{
		regexp.MustCompile(`(Authorization:\s+Bearer\s+)([a-zA-Z0-9._-]+)`),
		regexp.MustCompile(`(authorization:\s+bearer\s+)([a-zA-Z0-9._-]+)`),
		regexp.MustCompile(`(Authorization:\s+Basic\s+)([a-zA-Z0-9+/]+=*)`),
		regexp.MustCompile(`(authorization:\s+basic\s+)([a-zA-Z0-9+/]+=*)`),
	}

	for _, pattern := range tokenPatterns {
		s = pattern.ReplaceAllString(s, "${1}[REDACTED]")
	}

	// Phase 2: Replace API tokens in URLs/strings
	urlPatterns := []*regexp.Regexp{
		regexp.MustCompile(`sk-ant-ccsr-[a-zA-Z0-9_-]+`),
		regexp.MustCompile(`sk-ant-[a-zA-Z0-9_-]+`),
	}

	for _, pattern := range urlPatterns {
		s = pattern.ReplaceAllString(s, "[REDACTED]")
	}

	return s
}

// ServeHTTP handles incoming HTTP requests to the git proxy.
// It parses the git protocol URL path, validates the request, looks up
// the repository authentication, and forwards the request to the upstream
// git server (session ingress proxy).
//
// URL format expected: /git/{owner}/{repo}/{gitPath}
// The path is split by "/" with the expectation of at least 3 segments after /git/.
//
// Binary address: 0xae5580
// Source file: handler.go
func (h *handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Use the request context, or fall back to background context
	ctx := r.Context()
	if ctx == nil {
		ctx = context.Background()
	}

	// Get the URL path, stripping "/git/" prefix if present
	urlPath := r.URL.Path
	if len(urlPath) >= 5 && urlPath[:5] == "/git/" {
		urlPath = urlPath[5:]
	}

	// Split path into: owner/repo/gitPath (at most 3 parts)
	parts := strings.SplitN(urlPath, "/", 3)
	if len(parts) < 3 {
		h.logger.Error("invalid git path",
			"status", http.StatusBadRequest,
		)
		http.Error(w, "invalid git path", http.StatusBadRequest)
		return
	}

	owner := parts[0]
	repo := parts[1]
	gitPath := parts[2]

	// Strip ".git" suffix from repo if present
	if len(repo) >= 4 && repo[len(repo)-4:] == ".git" {
		repo = repo[:len(repo)-4]
	}

	// Check Authorization header
	authorization := r.Header.Get("Authorization")

	// Log the incoming request
	h.logger.Info("Received git request",
		"method", r.Method,
		"owner", owner,
		"repo", repo,
		"git_path", gitPath,
		"has_authorization", authorization != "",
	)

	// Check if this looks like a session ingress path
	if len(gitPath) >= 9 && gitPath[:9] == "session_i" {
		// Forward to session ingress proxy
		h.logger.Info("Forwarding request to session ingress proxy")
		h.forwardRequest(w, r, ctx, owner, repo, gitPath, authorization, "", "")
		return
	}

	// Build the repo key for auth lookup
	repoKey := fmt.Sprintf("%s/%s", owner, repo)

	// Look up authentication for this repo
	authToken, ok := h.server.repoAuths[repoKey]
	if !ok {
		h.logger.Error("no auth configured for repo",
			"repo", repoKey,
		)
		w.Header().Set("WWW-Authenticate", `Basic realm="Git Proxy"`)
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}

	// Forward the authenticated request
	h.forwardRequest(w, r, ctx, owner, repo, gitPath, authorization, authToken.Token, authToken.UpstreamURL)
}

// forwardRequest constructs and sends the proxied HTTP request to the upstream
// git server, then relays the response back to the client.
//
// It validates the git path, constructs the upstream URL by combining the
// session ingress URL with the URL-encoded path components, and copies
// the authorization header if present.
//
// Binary address: 0xae6660
// Source file: handler.go
func (h *handler) forwardRequest(
	w http.ResponseWriter,
	r *http.Request,
	ctx context.Context,
	owner, repo, gitPath, authorization, authToken, upstreamURL string,
) {
	// Validate git path
	if !isValidGitPath(gitPath) {
		h.logger.Error("invalid git path",
			"git_path", gitPath,
		)
		http.Error(w, "invalid git path", http.StatusBadRequest)
		return
	}

	// Log the forwarding operation
	h.logger.Info("Sending authenticated request to upstream",
		"owner", owner,
		"repo", repo,
	)

	// Build upstream URL with session_id as path component
	// Binary at 0xae6660 uses upstreamURL as baseURL when non-empty, and includes
	// session_id as a path component between baseURL and owner.
	baseURL := h.server.sessionIngressURL
	if upstreamURL != "" {
		baseURL = strings.TrimRight(upstreamURL, "/")
	}

	// URL format: baseURL/sessionID/owner/repo/gitPath
	targetURL := fmt.Sprintf("%s/%s/%s/%s/%s",
		baseURL,
		h.server.sessionID,
		owner,
		repo,
		gitPath,
	)

	// Create the upstream request
	req, err := http.NewRequestWithContext(ctx, r.Method, targetURL, r.Body)
	if err != nil {
		h.logger.Error("failed to create upstream request",
			"error", sanitizeError(err),
		)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}

	// Copy relevant headers
	if authorization != "" {
		req.Header.Set("Authorization", authorization)
	} else if authToken != "" {
		req.Header.Set("Authorization", "Bearer "+authToken)
	}

	// Copy Content-Type from original request
	if ct := r.Header.Get("Content-Type"); ct != "" {
		req.Header.Set("Content-Type", ct)
	}

	// Send the request
	resp, err := h.httpClient.Do(req)
	if err != nil {
		sanitizedErr := sanitizeError(err)
		h.logger.Error("upstream request failed",
			"error", sanitizedErr,
		)
		http.Error(w, sanitizedErr, http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	// Copy response headers
	for key, values := range resp.Header {
		for _, value := range values {
			w.Header().Add(key, value)
		}
	}

	// Write status code
	w.WriteHeader(resp.StatusCode)

	// Copy response body
	if _, err := io.Copy(w, resp.Body); err != nil {
		h.logger.Error("Error writing sanitized response body",
			"error", sanitizeError(err),
		)
	}
}
