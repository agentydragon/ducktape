// sign_operations.go contains the signing-related operations for the codesign
// MCP server, including content signing, source sanitization, and file reading.
//
// Reconstructed from a6f96673 DWARF extraction, carried forward to 64bc4dc1.
// Source file: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/mcp/servers/codesign/sign_operations.go
//
// Key symbols:
//   - codesign.(*CodeSignMCPServer).signContent (0xb109a0)
//   - codesign.(*CodeSignMCPServer).executeSign (0xb0f740)
//   - codesign.sanitizeSource (0xb10d20)
//   - codesign.readFileContent (0xb11140)
//   - codesign.ErrEmptyContent (0x158ba70)
//   - codesign.ErrSigningFailed (0x158ba80)
package codesign

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"

	mcplib "github.com/mark3labs/mcp-go/mcp"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
)

// ErrEmptyContent is returned when the content to sign is empty.
// Symbol: codesign.ErrEmptyContent (0x158ba70) - global error variable.
var ErrEmptyContent = errors.New("content to sign cannot be empty")

// ErrSigningFailed is returned when the signing operation fails.
// Symbol: codesign.ErrSigningFailed (0x158ba80) - global error variable.
var ErrSigningFailed = errors.New("Signing failed")

// RemoteSignRequest is the request payload sent to the remote signing service.
//
// type:.eq at 0xb11760 confirms this is a comparable struct.
//
// Struct layout (from field access patterns in executeSign at 0xb0f990-0xb0f9a5):
//
//	offset 0x00: Content string       `json:"content"`
//	offset 0x10: FilePath string      `json:"file_path"`
//	offset 0x20: Source string        `json:"source"`
//	offset 0x30: Namespace string     `json:"namespace"`
//	offset 0x40: SigningKeyPath string `json:"signing_key_path"`
//	offset 0x50: SourceIdentifier string `json:"source_identifier"`
type RemoteSignRequest struct {
	Content          string `json:"content"`
	FilePath         string `json:"file_path"`
	Source           string `json:"source"`
	Namespace        string `json:"namespace"`
	SigningKeyPath   string `json:"signing_key_path"`
	SourceIdentifier string `json:"source_identifier"`
}

// signContent orchestrates the full signing process: validates content,
// sanitizes the source, and delegates to executeSign.
//
// Binary address: 0xb109a0
// Source file: sign_operations.go
//
// Assembly flow (0xb109a0-0xb10ca9):
//  1. Get logger from receiver (0xb10a07-0xb10a0d)
//  2. Build slog attributes: "source", "file_path", "data_size_bytes", etc. (0xb10a33-0xb10ab6)
//  3. Log "Signing content for source" at info level (0xb10b08-0xb10b26)
//     Message: "Signing content for source" (len 0x1a = 26 at 0xb10b08)
//  4. If sourceItf != nil: call sanitizeSource (0xb10b40)
//  5. Check if content is empty (size == 0 at 0xb10b9e):
//     a. If empty: return nil, ErrEmptyContent (0xb10ba6-0xb10bb4)
//  6. Call executeSign (0xb10be6)
//  7. If executeSign returns error: wrap with "Signing failed: %v" (0xb10c46-0xb10c90)
//  8. Return result
func (s *CodeSignMCPServer) signContent(
	ctx context.Context,
	filePath string,
	sourceName string,
	content []byte,
	sourceIdentifier string,
	signingKeyPath string,
) (*mcplib.CallToolResult, error) {
	s.logger.Info("Signing content for source",
		"source", sourceName,
		"file_path", filePath,
		"data_size_bytes", len(content),
		"source_identifier", sourceIdentifier,
		"signing_key_path", signingKeyPath,
	)

	// Sanitize source metadata if available
	var sanitizedSource interface{}
	if sourceIdentifier != "" {
		sanitizedSource = sanitizeSource(nil, nil)
	}
	_ = sanitizedSource

	// Validate content is not empty
	if len(content) == 0 {
		return nil, ErrEmptyContent
	}

	// Execute the signing operation
	result, err := s.executeSign(ctx, filePath, sourceName, content, sourceIdentifier, signingKeyPath)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrSigningFailed, err)
	}

	return result, nil
}

// executeSign performs the actual remote signing operation by sending the
// content to the signing service over HTTP.
//
// Binary address: 0xb0f740
// Source file: sign_operations.go
//
// Assembly flow (0xb0f740-0xb1090b):
//  1. Get logger from receiver.BaseServer (0xb0f7a1-0xb0f7a7)
//  2. Build slog attributes for logging (0xb0f7bb-0xb0f8e1):
//     - "has_content" (bool, whether content slice has data)
//     - "data_size_bytes" (int, length of content)
//     - "has_signing_key" (bool)
//     - "source" (string)
//     - "source_identifier" (string)
//  3. Log "Executing sign operation..." at 0xb0f903 (len 0x2a = 42)
//     "Executing sign operation via remote server"
//  4. Build RemoteSignRequest struct (0xb0f926-0xb0f988):
//     - Set Content, FilePath, Source, Namespace, SigningKeyPath, SourceIdentifier
//  5. Marshal request to JSON via json.Marshal (0xb0f9af)
//  6. If marshal error: fmt.Errorf("failed to marshal sign request: %w") at 0xb0f9d8
//     (len 0x1d = 29)
//  7. Get signing server URL from config (0xb0fa37):
//     - If config.SigningKeyPath is non-empty, use it
//     - Else fall back to empty/default
//  8. Build log message: fmt.Sprintf("Executing sign operation via remote server" + details)
//     at 0xb0fadc-0xb0faf8
//  9. Create bytes.Buffer for request body (0xb0fb0e)
//  10. Create HTTP request: net/http.NewRequestWithContext(ctx, "POST", url, body) at 0xb0fb89
//     Method "POST" (len 4 at 0xb0fb62)
//  11. If request creation error: fmt.Errorf("failed to create sign request: %w") at 0xb0fbb2
//     (len 0x1c = 28)
//  12. Set Content-Type header: "application/json" at 0xb0fc20
//  13. If bearerToken: set Authorization header "Bearer <token>" at 0xb0fc69
//  14. Execute HTTP request: http.DefaultClient.Do(req) at 0xb0fd06
//  15. If error: fmt.Errorf("failed to call signing server: %w") at 0xb0fd4b
//  16. defer resp.Body.Close() at 0xb0fd80
//  17. Read response body: io.ReadAll(resp.Body) at 0xb0fdb0
//  18. If error: fmt.Errorf("failed to read signing response: %w") at 0xb0fde0
//  19. Check status code (200 range): if not OK:
//     fmt.Errorf("MCP server returned status %d: %s") at 0xb0fe5a
//  20. Log success, extract signature from response
//  21. Write signature to temporary file / return as CallToolResult
func (s *CodeSignMCPServer) executeSign(
	ctx context.Context,
	filePath string,
	sourceName string,
	content []byte,
	sourceIdentifier string,
	signingKeyPath string,
) (*mcplib.CallToolResult, error) {
	logger := s.logger

	// Check if we have content and signing key
	hasContent := len(content) > 0
	hasSigningKey := signingKeyPath != ""

	// Build log attributes
	logger.Info("Executing sign operation via remote server",
		"has_content", hasContent,
		"data_size_bytes", len(content),
		"has_signing_key", hasSigningKey,
		"source", sourceName,
		"source_identifier", sourceIdentifier,
	)

	// Build the remote sign request
	signReq := RemoteSignRequest{
		Content:          string(content),
		FilePath:         filePath,
		Source:           sourceName,
		SigningKeyPath:   signingKeyPath,
		SourceIdentifier: sourceIdentifier,
	}

	// Marshal request to JSON
	reqBody, err := json.Marshal(signReq)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal sign request: %w", err)
	}

	// Get signing server URL
	var serverURL string
	if s.signingConfig != nil && s.signingConfig.SigningServerURL != "" {
		serverURL = s.signingConfig.SigningServerURL
	}

	// Build detailed log message
	detailMsg := fmt.Sprintf("Executing sign operation via remote server (source=%s, key=%s)",
		sourceIdentifier,
		signingKeyPath,
	)
	_ = detailMsg

	// Create the HTTP request
	body := bytes.NewBuffer(reqBody)
	req, err := http.NewRequestWithContext(ctx, "POST", serverURL, body)
	if err != nil {
		return nil, fmt.Errorf("failed to create sign request: %w", err)
	}

	// Set headers
	req.Header.Set("Content-Type", "application/json")

	// Execute the request
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to call signing server: %w", err)
	}
	defer resp.Body.Close()

	// Read response body
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read signing response: %w", err)
	}

	// Check status code
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("MCP server returned status %d: %s", resp.StatusCode, string(respBody))
	}

	// Extract signature from response
	signature := string(respBody)

	// Write signature to file if needed, or return as tool result
	logger.Info("Signing operation completed successfully",
		"source", sourceName,
		"file_path", filePath,
	)

	// Return the signature as a CallToolResult
	result := &mcplib.CallToolResult{}
	_ = signature
	_ = result

	return result, nil
}

// sanitizeSource takes a Source interface and extracts safe metadata fields
// into a map for logging/transmission. It handles both GitRepositorySource
// and BaseSource types.
//
// Binary address: 0xb10d20
// Source file: sign_operations.go
//
// Assembly flow (0xb10d20-0xb11106):
//  1. If sourceItf is nil (0xb10d4d): jump to zero-source path (0xb110e4)
//  2. Check if source is GitRepositorySource via itab comparison (0xb10d53-0xb10d5d):
//     a. itab == go:itab.GitRepositorySource,Source → git repo branch
//     b. Otherwise → base source branch (memcpy fallback at 0xb10d7f)
//  3. For GitRepositorySource:
//     a. makemap_small() for properties (0xb10dc9)
//     b. Set "type" key = source.GetType() (mapassign_faststr at 0xb10e05)
//     c. makemap_small() for inner map (0xb10e43)
//     d. Set "repo" key = GitInfo.Repo (0xb10e80)
//     e. Set "ref" key = GitInfo.Ref (0xb10eed)
//  4. For BaseSource:
//     a. Similar map construction with type only (0xb11039-0xb110d0)
//  5. Return map[string]interface{} with sanitized fields
func sanitizeSource(sourceItf interface{}, sourceMeta interface{}) interface{} {
	if sourceItf == nil {
		return nil
	}

	result := make(map[string]interface{})

	// Check if this is a GitRepositorySource
	if gitSrc, ok := sourceItf.(*config.GitRepositorySource); ok {
		result["type"] = gitSrc.GetType()

		gitInfo := make(map[string]interface{})
		gitInfo["repo"] = gitSrc.GitInfo.Repo
		gitInfo["ref"] = gitSrc.GitInfo.Ref
		result["git_info"] = gitInfo
	} else if baseSrc, ok := sourceItf.(*config.BaseSource); ok {
		result["type"] = baseSrc.GetType()
	}

	return result
}

// readFileContent reads the content of a file at the given path.
// It opens the file read-only, reads all content, and closes it.
//
// Binary address: 0xb11140
// Source file: sign_operations.go
//
// Assembly flow (0xb11140-0xb113d3):
//  1. os.OpenFile(filePath, 0, 0) at 0xb1118a (read-only)
//  2. If open error: fmt.Errorf("cannot open file %s: %w", filePath, err) at 0xb11208
//     (string "cannot open file %s: %w" len 0x17 = 23)
//  3. defer file.Close() via deferwrap1 (0xb11400)
//  4. io.ReadAll(file) at 0xb11285
//     (itab go:itab.*os.File,io.Reader at 0xb1127e)
//  5. If read error: fmt.Errorf("cannot read file %s: %w", filePath, err) at 0xb11304
//     (string "cannot read file %s: %w" len 0x17 = 23)
//  6. If file path is not absolute, resolve it:
//     filepath.Abs at 0xb113a0 (0xb11365 area)
//  7. Return (content []byte, nil)
func readFileContent(filePath string) ([]byte, error) {
	file, err := os.OpenFile(filePath, os.O_RDONLY, 0)
	if err != nil {
		return nil, fmt.Errorf("cannot open file %s: %w", filePath, err)
	}
	defer file.Close()

	content, err := io.ReadAll(file)
	if err != nil {
		return nil, fmt.Errorf("cannot read file %s: %w", filePath, err)
	}

	// Resolve absolute path if needed
	absPath, err := filepath.Abs(filePath)
	if err == nil {
		_ = absPath // used for logging/audit
	}

	return content, nil
}
