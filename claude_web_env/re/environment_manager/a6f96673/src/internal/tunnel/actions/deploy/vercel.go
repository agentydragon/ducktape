// Reconstructed from environment-manager binary (Build ID: a6f96673)
// Source: internal/tunnel/actions/deploy/vercel.go
// Module: github.com/anthropics/anthropic/api-go/environment-manager

package deploy

import (
	"bytes"
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// FileEntry represents a file to be deployed, with its path, SHA hash, size, and content.
// Based on binary analysis, struct layout includes:
// - Path (string): ptr + len (16 bytes)
// - SHA (string): ptr + len (16 bytes)
// - Content ([]byte): ptr + len + cap (24 bytes)
// - Size (int64): 8 bytes
type FileEntry struct {
	Path    string
	SHA     string
	Content []byte
	Size    int64
}

// VercelClient handles communication with the Vercel API for deployments.
type VercelClient struct {
	Token   string        // offset 0x00 (ptr + len)
	TeamID  string        // offset 0x10 (ptr + len)
	BaseURL string        // offset 0x20 (ptr + len)
	Timeout time.Duration // offset 0x28 (default 60s = 0xdf8475800)
}

// Deployment represents a Vercel deployment response.
type Deployment struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	URL  string `json:"url"`
}

// ValidateProjectDir validates that the given directory path is under
// /home/project/. Cleans the path first using filepath.Clean,
// then checks if it starts with "/home/project/" (13 chars = 0x0d).
//
// Returns nil on success, or an error like:
//
//	"invalid project directory %q: must be under %s"
//
// Binary address: 0xb41b80
func ValidateProjectDir(dir string) error {
	cleaned := filepath.Clean(dir)

	prefix := "/home/project/" // 0x0d = 13 chars (checked without trailing slash actually)
	if len(cleaned) >= 13 && cleaned[:13] == "/home/project" {
		return nil
	}

	return fmt.Errorf("invalid project directory %q: must be under %s", cleaned, prefix)
}

// ProjectName derives a project name from a path. It:
//  1. Strips known prefixes: "/home/project/" (len 16) and another (len 8)
//  2. Truncates to 20 characters
//  3. Lowercases the string
//  4. Iterates runes: keeps [a-z] (0x61-0x7a, check SI<=0x19), [0-9] (0x30-0x39, check SI<=0x9),
//     and '-' (0x2d), replacing all other characters with '-'
//  5. Prepends "duck-" (5 chars)
//
// Binary address: 0xb41c60
func ProjectName(path string) string {
	// Strip known prefixes
	prefixes := []struct {
		s   string
		len int
	}{
		{"/home/project/", 16}, // 0x10 = 16
		{"project/", 8},        // 0x08 = 8
	}

	for _, p := range prefixes {
		if len(path) >= p.len && path[:p.len] == p.s {
			path = path[p.len:]
			break
		}
	}

	// Truncate to 20 characters
	if len(path) > 20 { // 0x14 = 20
		path = path[:20]
	}

	// Lowercase the string
	path = strings.ToLower(path)

	// Replace invalid characters with '-', keeping [a-z], [0-9], and '-'
	var b strings.Builder
	for _, r := range path {
		if r >= 'a' && r <= 'z' { // 0x61-0x7a, check (r - 0x61) <= 0x19
			b.WriteRune(r)
		} else if r >= '0' && r <= '9' { // 0x30-0x39, check (r - 0x30) <= 0x9
			b.WriteRune(r)
		} else if r == '-' { // 0x2d
			b.WriteRune(r)
		} else {
			b.WriteRune('-') // 0x2d
		}
	}

	// Prepend "duck-" (5 chars)
	return "duck-" + b.String()
}

// CollectFiles walks the given directory using path/filepath.WalkDir and
// collects all files into a slice of FileEntry. Also builds a SHA-indexed map
// for deduplication during upload.
//
// Returns: (files []FileEntry, shaMap map[string]string, error)
// On error: "failed to collect files: %w"
//
// Binary address: 0xb3f240
func CollectFiles(dir string) ([]FileEntry, error) {
	var files []FileEntry
	shaMap := make(map[string]int64)

	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return fmt.Errorf("walk error for %s: %w", path, err)
		}

		// Skip directories
		if d.IsDir() {
			return nil
		}

		// Read file content
		content, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("failed to read %s: %w", path, err)
		}

		// Check cumulative size doesn't exceed 100MB (0x6400000 bytes)
		totalSize := shaMap[""] // Map stores total at empty key based on binary
		totalSize += int64(len(content))
		if totalSize > 0x6400000 {
			return fmt.Errorf("total file size exceeds limit")
		}
		shaMap[""] = totalSize

		// Compute relative path from dir
		relPath, err := filepath.Rel(dir, path)
		if err != nil {
			return fmt.Errorf("failed to compute relative path for %s: %w", path, err)
		}

		// Compute SHA1 hash (binary uses crypto/sha1.Sum, not SHA256)
		hash := sha1.Sum(content)
		hashHex := hex.EncodeToString(hash[:])

		// Append FileEntry with path, SHA, content, and size
		files = append(files, FileEntry{
			Path:    relPath,
			SHA:     hashHex,
			Content: content,
			Size:    int64(len(content)),
		})

		// Store hash -> size in map for deduplication
		shaMap[hashHex] = int64(len(content))

		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("failed to collect files: %w", err)
	}

	return files, nil
}

// CreateDeployment creates a new Vercel deployment via the API.
// Builds a request body with keys:
//   - "name" (string) - project name
//   - "project" (string) - project name
//   - "target" (bool true) - deployment target
//   - "files" (array) - file entries
//   - "gitSource" (object) with keys:
//   - "repoSlug" (null)
//   - "buildCommand" (string)
//   - "outputDirectory" (string)
//   - "installCommand" (string)
//
// Sends POST request with Authorization: Bearer <token>.
// On marshal error: "failed to marshal deployment request: %w"
//
// Binary address: 0xb3fa80
func (c *VercelClient) CreateDeployment(
	ctx context.Context,
	name string,
	files []FileEntry,
	commitSHA string,
) (*Deployment, error) {
	// Build request body
	reqBody := map[string]interface{}{
		"name":    name,  // len=4
		"project": name,  // len=7
		"target":  true,  // len=6
		"files":   files, // len=5
		"gitSource": map[string]interface{}{
			"repoSlug":        nil, // len=9
			"buildCommand":    "",  // len=12
			"outputDirectory": "",  // len=15
			"installCommand":  "",  // len=14
		},
	}

	jsonBody, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal deployment request: %w", err)
	}

	url := c.BaseURL + "/v13/deployments"
	if c.TeamID != "" {
		url += "?teamId=" + c.TeamID
	}

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewReader(jsonBody))
	if err != nil {
		return nil, fmt.Errorf("failed to create deployment request: %w", err)
	}

	req.Header.Set("Authorization", "Bearer "+c.Token)
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: c.Timeout}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("deployment request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read deployment response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("deployment failed with status %d: %s", resp.StatusCode, string(body))
	}

	var deployment Deployment
	if err := json.Unmarshal(body, &deployment); err != nil {
		return nil, fmt.Errorf("failed to parse deployment response: %w", err)
	}

	return &deployment, nil
}

// UploadFile uploads a single file to Vercel.
// Constructs URL: base + "/v2/files" (len=9), optionally appending "?teamId=" + teamId.
// Creates bytes.Reader for file content.
// Sets headers:
//   - "Authorization" (len=13) = "Bearer " + token
//   - "Content-Type" (len=12) = "application/octet-stream" (len=24)
//   - "x-vercel-digest" (len=15) = file SHA
//
// On error: "failed to create upload request: %w"
//
// Binary address: 0xb409a0
func (c *VercelClient) UploadFile(ctx context.Context, file FileEntry) error {
	url := c.BaseURL + "/v2/files" // len=9
	if c.TeamID != "" {
		url += "?teamId=" + c.TeamID
	}

	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewReader(file.Content))
	if err != nil {
		return fmt.Errorf("failed to create upload request: %w", err)
	}

	req.Header.Set("Authorization", "Bearer "+c.Token)         // len=13
	req.Header.Set("Content-Type", "application/octet-stream") // len=12, value len=24
	req.Header.Set("x-vercel-digest", file.SHA)                // len=15

	client := &http.Client{Timeout: c.Timeout}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("failed to upload file: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("upload failed with status %d: %s", resp.StatusCode, string(body))
	}

	return nil
}

// WaitForReady polls the Vercel API until the deployment is ready or fails.
// Uses time.Now() + deadline for timeout loop.
//
// In loop:
//  1. Checks context cancellation via selectnbrecv
//  2. Constructs URL with deployment ID and name params via fmt.Sprintf,
//     appends "?teamId=" if present
//  3. Makes GET request with Authorization header
//  4. Reads response body with io.ReadAll, closes body
//  5. Checks status code:
//     - 200 (0xc8) = parse response for state (checks for READY/ERROR/CANCELED)
//     - 429 (0x1ad) or >=500 (0x1f4) = retry after 2s timer (0x77359400 ns)
//     - other = error
//  6. Uses selectgo with 2 cases (context done vs timer) for retry wait
//
// Binary address: 0xb411c0
func (c *VercelClient) WaitForReady(ctx context.Context, deployment *Deployment) (string, error) {
	deadline := time.Now().Add(c.Timeout)

	for {
		// Check context cancellation
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		default:
		}

		// Check deadline
		if time.Now().After(deadline) {
			return "", fmt.Errorf("deployment timed out waiting for ready state")
		}

		// Construct URL for checking deployment status
		url := fmt.Sprintf("%s/v13/deployments/%s", c.BaseURL, deployment.ID)
		if c.TeamID != "" {
			url += "?teamId=" + c.TeamID
		}

		req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
		if err != nil {
			return "", fmt.Errorf("failed to create status request: %w", err)
		}

		req.Header.Set("Authorization", "Bearer "+c.Token)

		client := &http.Client{Timeout: c.Timeout}
		resp, err := client.Do(req)
		if err != nil {
			return "", fmt.Errorf("status request failed: %w", err)
		}

		body, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			return "", fmt.Errorf("failed to read status response: %w", err)
		}

		switch {
		case resp.StatusCode == 200: // 0xc8
			// Parse response for readyState
			var status struct {
				ReadyState string `json:"readyState"`
				URL        string `json:"url"`
			}
			if err := json.Unmarshal(body, &status); err != nil {
				return "", fmt.Errorf("failed to parse status response: %w", err)
			}

			switch status.ReadyState {
			case "READY":
				return "https://" + status.URL, nil
			case "ERROR", "CANCELED":
				return "", fmt.Errorf("deployment failed with state: %s", status.ReadyState)
			}
			// Still building, continue polling

		case resp.StatusCode == 429 || resp.StatusCode >= 500: // 0x1ad, 0x1f4
			// Rate limited or server error, retry after delay

		default:
			return "", fmt.Errorf("unexpected status code %d: %s", resp.StatusCode, string(body))
		}

		// Wait 2 seconds before retrying (0x77359400 ns = 2,000,000,000 ns)
		timer := time.NewTimer(2 * time.Second)
		select {
		case <-ctx.Done():
			timer.Stop()
			return "", ctx.Err()
		case <-timer.C:
		}
	}
}
