// Reconstructed from a6f96673 DWARF extraction, carried forward to 64bc4dc1.
// Source: internal/claude/session_urls.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/claude/session_urls.go
//
// Key symbols:
//   - claude.buildSessionURLs (0xafafc0)
//
// This file contains the buildSessionURLs function which constructs session
// URLs from a base URL, stripping scheme prefixes and path separators.

package claude

import (
	"strings"
)

// buildSessionURLs constructs session URLs from a base URL string, session ID,
// and additional parameters. It handles both "https://" and "http://" scheme
// prefixes, strips them if present, and constructs URLs based on path patterns.
//
// Binary address: 0xafafc0
// Source lines: 17-74
//
// Assembly flow:
//  1. Check if URL starts with "https://" (8 chars) at line 17
//  2. If yes: strip prefix, set hasScheme=true at line 22-23
//  3. Else check if URL starts with "http://" (7 chars) at line 24
//  4. If yes: strip prefix, set hasScheme=true
//  5. Find "/" separator in remaining URL at line 29-30
//  6. If found and index <= len: split host from path at line 31
//  7. Find "?" separator at line 33-34
//  8. If found and index <= current len: truncate at query string
//  9. Check for "localhost" prefix (9 chars) at line 37-38
//  10. Check for "127.0.0.1" prefix at line 42
//  11. Build formatted URL: "%s://%s/v1/code/sessions/%s" at line 46-74
//     using scheme ("https" or "http"), host, and session ID
//
// String references:
//   - "https://" (8 bytes) - scheme prefix check
//   - "http://" (7 bytes) - scheme prefix check
//   - "/" (1 byte) - path separator
//   - "?" (1 byte) - query string separator
//   - "https" (5 bytes) - default scheme
//   - "http" (4 bytes) - plaintext scheme
//   - "%s://%s/v1/code/sessions/%s" (26 bytes) - URL format
func buildSessionURLs(baseURL string, sessionID string, apiKey string, workerID string, useHTTP bool) string {
	hasHTTPS := false

	// Strip scheme prefix
	if len(baseURL) >= 8 && strings.HasPrefix(baseURL, "https://") {
		baseURL = baseURL[8:]
		hasHTTPS = true
	} else if len(baseURL) >= 7 && strings.HasPrefix(baseURL, "http://") {
		baseURL = baseURL[7:]
	}

	// Find path separator and strip path
	host := baseURL
	if idx := strings.Index(baseURL, "/"); idx != -1 {
		if idx <= len(baseURL) {
			host = baseURL[:idx]
		}
	}

	// Strip query string
	if idx := strings.Index(host, "?"); idx != -1 {
		if idx <= len(host) {
			host = host[:idx]
		}
	}

	// Determine scheme based on host and flags
	isLocal := strings.HasPrefix(host, "localhost") || strings.HasPrefix(host, "127.0.0.1")

	scheme := "https"
	if useHTTP || (isLocal && !hasHTTPS) {
		scheme = "http"
	}

	// Build session URL
	// Format: "%s://%s/v1/code/sessions/%s"
	return scheme + "://" + host + "/v1/code/sessions/" + sessionID
}
