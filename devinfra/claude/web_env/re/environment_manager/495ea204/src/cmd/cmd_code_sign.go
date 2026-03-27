// Reconstructed from binary: environment-manager (Build ID 495ea204)
// Source: cmd/cmd_code_sign.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/cmd
// Updated in a6f96673: code-sign subcommand is now inlined in main.go,
// but the underlying functions remain in this file.
// Carried forward unchanged to 495ea204.

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/rand"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// calculateBackoff computes the backoff duration for a given retry attempt
// using exponential backoff with jitter.
//
// Binary: 0xb710c0 - cmd.calculateBackoff
// Source: cmd/cmd_code_sign.go
//
// Parameters (register-based Go ABI):
//
//	AX = attempt number (0-based)
//	BX = base duration (nanoseconds)
//	CX = min backoff (nanoseconds)
//	DI = max backoff (nanoseconds)
//	X0 = jitter factor (float64)
//
// Flow:
//  1. If attempt <= 0: return min backoff (CX)
//  2. Compute backoff = base * pow(factor, attempt-1)
//  3. Cap at max backoff (DI)
//  4. Add random jitter: rand.Float64() * 0.3 * backoff
//  5. Return backoff + jitter as int64 nanoseconds
func calculateBackoff(attempt int, base time.Duration, minBackoff time.Duration, maxBackoff time.Duration, factor float64) time.Duration {
	if attempt <= 0 {
		return minBackoff
	}

	backoff := float64(base) * math.Pow(factor, float64(attempt-1))
	if backoff > float64(maxBackoff) {
		backoff = float64(maxBackoff)
	}

	// Add jitter: up to 30% of the backoff duration (0x3fd3333333333333 = 0.3)
	jitter := rand.Float64() * 0.3 * backoff
	return time.Duration(backoff + jitter)
}

// RunCodeSignFromMain is the exported wrapper called from main.go's inline
// code-sign command. Since a6f96673, the code-sign subcommand is defined inline
// in main.go rather than via a separate AddCodeSignCommand function.
//
// Binary: cmd.runCodeSign is still at the same address, just called differently.
func RunCodeSignFromMain(ctx context.Context, args []string) error {
	return runCodeSign(ctx, args)
}

// runCodeSign starts the code-sign mode. This function is the entry point
// when the binary is invoked with code-signing arguments. It parses the
// argument list looking for "-Y sign" (SSH-style signing) and dispatches
// to handleSSHSign.
//
// Binary: 0xb711a0 - cmd.runCodeSign
// Source: cmd/cmd_code_sign.go
//
// Parameters:
//
//	AX = unused (0)
//	BX = args slice data pointer ([]string)
//	CX = args slice length
//
// Flow:
//  1. Iterates over args looking for "-Y" (0x592d) followed by "sign" (0x6e676973)
//  2. Collects remaining args into a slice
//  3. If "-Y sign" found: calls handleSSHSign with the collected args
//  4. If not found: returns error
//     "unsupported code-sign operation: currently only SSH-style signing (-Y sign) is supported"
func runCodeSign(ctx context.Context, args []string) error {
	var remaining []string
	isSign := false

	for i := 0; i < len(args); i++ {
		arg := args[i]

		// Check for "-Y" flag (2-byte string matching 0x592d = "-Y")
		if len(arg) == 2 && arg == "-Y" {
			if i+1 < len(args) {
				nextArg := args[i+1]
				// Check for "sign" (4-byte string matching 0x6e676973)
				if len(nextArg) == 4 && nextArg == "sign" {
					isSign = true
					i++ // skip "sign"
					continue
				}
			}
		}

		remaining = append(remaining, arg)
	}

	if isSign {
		return handleSSHSign(remaining)
	}

	return fmt.Errorf("unsupported code-sign operation: currently only SSH-style signing (-Y sign) is supported")
}

// handleSSHSign handles SSH signing requests. It parses flags from the
// argument list, reads configuration, calls the MCP server's sign_file tool,
// and verifies the signature file was created.
//
// Binary: 0xb713a0 - cmd.handleSSHSign
// Source: cmd/cmd_code_sign.go
//
// Parameters:
//
//	AX = args slice data pointer ([]string)
//	BX = args slice length
//	CX = args slice capacity
//
// Parsed flags:
//
//	-f <path>     : file to sign (required)
//	-n <namespace>: namespace (logged to stderr, ignored for signing)
//	other flags   : consumed with their values
//	positional    : treated as buffer path to sign
//
// Flow:
//  1. Parse -f (file path) and -n (namespace) flags from args
//  2. If no file specified: error "no file specified to sign"
//  3. If file path not absolute, convert via filepath.Abs
//  4. Log namespace to stderr: "Debug: Namespace set to %q (ignored)\n"
//  5. Log file to stderr: "Debug: Key file set to %q (ignored, using server key)\n"
//  6. Get working directory via os.Getwd
//  7. Call getGitObjectFormat(cwd) to detect sha1/sha256
//  8. Call readCodeSignConfig() for port/token
//  9. Build timestamp ID via fmt.Sprintf("sign-%d", time.Now().UnixNano())
//  10. Build arguments map: file_path, repo_directory, git_object_format
//  11. Call callMCPServer with method="tools/call", tool="sign_file"
//  12. Parse MCPToolResult response
//  13. If IsError: return "signing failed: <text>" or "signing failed: unknown error"
//  14. Verify <file_path>.sig exists via os.Stat
//  15. If not exists: return "signature file not created: <path>"
func handleSSHSign(args []string) error {
	var filePath string
	var namespace string
	var namespaceFlagVal string

	for i := 0; i < len(args); i++ {
		arg := args[i]

		// Check if arg starts with '-' (flag)
		if len(arg) > 0 && arg[0] == '-' {
			// Check for -f flag
			if len(arg) == 2 && arg == "-f" {
				if i+1 >= len(args) {
					return fmt.Errorf("missing argument for -f flag")
				}
				filePath = args[i+1]
				i++
				continue
			}

			// Check for -n flag
			if len(arg) == 2 && arg == "-n" {
				if i+1 >= len(args) {
					return fmt.Errorf("missing argument for -n flag")
				}
				namespaceFlagVal = args[i+1]
				i++
				continue
			}

			// Unknown flag - check if next arg looks like a value (not a flag)
			if i+1 < len(args) {
				nextArg := args[i+1]
				if len(nextArg) > 0 && nextArg[0] == '-' {
					// Next arg is a flag, this flag has no value
					continue
				}
				// Skip the value argument
				i++
			}
		} else {
			// Positional argument: the buffer to sign
			if namespace == "" {
				namespace = arg
			}
		}
	}

	if filePath == "" {
		return fmt.Errorf("no file specified to sign")
	}

	// Convert to absolute path if relative
	if filePath[0] != '/' {
		absPath, err := filepath.Abs(filePath)
		if err != nil {
			return fmt.Errorf("failed to get absolute path for %s: %w", filePath, err)
		}
		filePath = absPath
	}

	// Log namespace if set (to stderr, for debugging)
	if len(namespaceFlagVal) > 0 {
		fmt.Fprintf(os.Stderr, "Debug: Namespace set to %q (ignored)\n", namespaceFlagVal)
	}

	// Log file path (to stderr, for debugging)
	if len(namespace) > 0 {
		fmt.Fprintf(os.Stderr, "Debug: Key file set to %q (ignored, using server key)\n", namespace)
	}

	// Get working directory
	cwd, err := os.Getwd()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Warning: could not determine working directory: %v\n", err)
		cwd = ""
	}

	// Get git object format (sha1 or sha256)
	objectFormat := getGitObjectFormat(cwd)

	// Read code signing configuration (port + token from env vars)
	config, configErr := readCodeSignConfig()
	if configErr != nil {
		return fmt.Errorf("failed to read codesign server configuration: %w", configErr)
	}

	// Create timestamp-based request ID: "sign-<unixnano>"
	now := time.Now()
	requestID := fmt.Sprintf("sign-%d", now.UnixNano())

	// Build arguments map for the sign_file tool
	arguments := make(map[string]interface{})
	arguments["file_path"] = filePath
	arguments["repo_directory"] = cwd
	arguments["git_object_format"] = objectFormat

	// Call the MCP server with tools/call method
	result, callErr := callMCPServer(config, "2.0", requestID, "tools/call", "sign_file", arguments)
	if callErr != nil {
		return fmt.Errorf("failed to call MCP server: %w", callErr)
	}

	// Parse the tool result
	var toolResult MCPToolResult
	if err := json.Unmarshal(result, &toolResult); err != nil {
		return fmt.Errorf("failed to parse tool result: %w", err)
	}

	// Check if the signing operation reported an error
	if toolResult.IsError {
		var errText string
		if len(toolResult.Content) > 0 {
			errText = toolResult.Content[0].Text
		} else {
			errText = "unknown error"
		}
		return fmt.Errorf("signing failed: %s", errText)
	}

	// Verify signature file was created at <file>.sig
	sigPath := filePath + ".sig"
	if _, statErr := os.Stat(sigPath); os.IsNotExist(statErr) {
		return fmt.Errorf("signature file not created: %s", sigPath)
	}

	return nil
}

// readCodeSignConfig reads the code signing configuration from environment
// variables CODESIGN_MCP_PORT and CODESIGN_MCP_TOKEN.
//
// Binary: 0xb71d80 - cmd.readCodeSignConfig
// Source: cmd/cmd_code_sign.go
//
// Returns:
//
//	AX = *CodeSignConfig (nil on error)
//	BX = error interface type
//	CX = error interface data
//
// Flow:
//  1. Read CODESIGN_MCP_PORT env var (string at 0xe0f0ae, len 17)
//  2. Read CODESIGN_MCP_TOKEN env var (string at 0xe1004f, len 18)
//  3. If either is empty: return error with full diagnostic message
//  4. Parse port string as integer via fmt.Sscanf("%d", &port)
//  5. If parse error or port <= 0: return same error
//  6. Return &CodeSignConfig{Port: port, Token: token}, nil
func readCodeSignConfig() (*CodeSignConfig, error) {
	portStr := os.Getenv("CODESIGN_MCP_PORT")
	token := os.Getenv("CODESIGN_MCP_TOKEN")

	if portStr == "" || token == "" {
		return nil, fmt.Errorf("CODESIGN_MCP_PORT and CODESIGN_MCP_TOKEN environment variables not set: make sure they are exported in your shell")
	}

	var port int
	_, err := fmt.Sscanf(portStr, "%d", &port)
	if err != nil || port <= 0 {
		return nil, fmt.Errorf("CODESIGN_MCP_PORT and CODESIGN_MCP_TOKEN environment variables not set: make sure they are exported in your shell")
	}

	return &CodeSignConfig{
		Port:  port,
		Token: token,
	}, nil
}

// getGitObjectFormat determines the git object format (sha1 or sha256) for
// the repository in the given directory.
//
// Binary: 0xb71f00 - cmd.getGitObjectFormat
// Source: cmd/cmd_code_sign.go
//
// Parameters:
//
//	AX = directory path (data ptr)
//	BX = directory path (length)
//
// Returns:
//
//	AX = format string data ("sha1" or "sha256")
//	BX = format string length (4 or 6)
//
// Flow:
//  1. If dir is empty (BX==0): return "sha1"
//  2. exec.Command("git", "rev-parse", "--show-object-format") with Dir set
//  3. cmd.Output() to capture stdout
//  4. If error: return "sha1"
//  5. strings.TrimSpace the output
//  6. If output == "sha256" (6 bytes, 0x32616873 + 0x3635): return "sha256"
//  7. Otherwise: return "sha1"
func getGitObjectFormat(dir string) string {
	if dir == "" {
		return "sha1"
	}

	cmd := exec.Command("git", "rev-parse", "--show-object-format")
	cmd.Dir = dir
	output, err := cmd.Output()
	if err != nil {
		return "sha1"
	}

	result := strings.TrimSpace(string(output))
	if result == "sha256" {
		return "sha256"
	}
	return "sha1"
}

// doSingleAttempt performs a single HTTP POST request to the MCP server
// and returns the parsed response content.
//
// Binary: 0xb72060 - cmd.doSingleAttempt
// Source: cmd/cmd_code_sign.go
//
// deferwrap1 at 0xb72a40 handles deferred resp.Body.Close().
//
// Parameters (register-based ABI):
//
//	AX = context
//	BX = URL string
//	CX = URL length
//	DI = body slice data
//	SI = body slice length
//	R8 = body slice capacity
//	R9 = token string
//	R10 = token length
//
// Flow:
//  1. Create bytes.NewReader from body (offset 0x00: data, 0x08: len, 0x10: cap)
//     Reader has pos=0 (offset 0x18), prevRune=-1 (offset 0x20)
//  2. http.NewRequestWithContext(ctx, "POST", url, reader)
//  3. Set req.Header "Content-Type" = "application/json"
//  4. Set req.Header "Authorization" = fmt.Sprintf("Bearer %s", token)
//  5. httpClient.Do(req)
//  6. defer resp.Body.Close()
//  7. io.ReadAll(resp.Body)
//  8. Check resp.StatusCode == 200; if not: error with status + body
//  9. json.Unmarshal into MCPResponse
//  10. If response.Error != nil: return fmt.Errorf("MCP error: %s", error.Message)
//  11. If len(Content) > 0: re-marshal Content+IsError and return
//  12. Otherwise: return "no result in MCP response"
func doSingleAttempt(ctx context.Context, url string, body []byte, token string, httpClient *http.Client) ([]byte, error) {
	reader := bytes.NewReader(body)
	req, err := http.NewRequestWithContext(ctx, "POST", url, reader)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	// Set headers
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", token))

	// Execute request
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	// Read response body
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}

	// Check status code
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("MCP server returned status %d: %s", resp.StatusCode, string(respBody))
	}

	// Parse JSON-RPC response
	var mcpResp MCPResponse
	if err := json.Unmarshal(respBody, &mcpResp); err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	// Check for MCP-level error
	if mcpResp.Error != nil {
		return nil, fmt.Errorf("MCP error: %s", mcpResp.Error.Message)
	}

	// Return content as re-serialized result
	if len(mcpResp.Content) > 0 {
		result, err := json.Marshal(MCPToolResult{
			Content: mcpResp.Content,
			IsError: mcpResp.IsError,
		})
		if err != nil {
			return result, nil
		}
		return result, nil
	}

	return nil, fmt.Errorf("no result in MCP response")
}

// doAttemptsWithRetry retries doSingleAttempt with exponential backoff.
// Logs retry attempts to stderr and returns the last error on exhaustion.
//
// Binary: 0xb72aa0 - cmd.doAttemptsWithRetry
// Source: cmd/cmd_code_sign.go
//
// Flow:
//  1. Loop from attempt=0 to maxAttempts (inclusive):
//     a. Call doSingleAttempt
//     b. On success: return result immediately
//     c. If more attempts remain (attempt < maxAttempts):
//     - Calculate backoff via calculateBackoff(attempt+1, ...)
//     - Log to stderr: "MCP server request failed (attempt %d/%d), retrying in %v: %v\n"
//     - time.Sleep(backoff)
//     d. Continue loop
//  2. If all attempts exhausted: return fmt.Errorf("MCP server request failed after %d retries: %w", ...)
func doAttemptsWithRetry(ctx context.Context, url string, body []byte, token string, httpClient *http.Client, maxAttempts int, baseBackoff time.Duration, maxBackoff time.Duration, factor float64) ([]byte, error) {
	var lastErr error

	for attempt := 0; attempt <= maxAttempts; attempt++ {
		result, err := doSingleAttempt(ctx, url, body, token, httpClient)
		if err == nil {
			return result, nil
		}

		lastErr = err

		if attempt < maxAttempts {
			backoff := calculateBackoff(attempt+1, baseBackoff, baseBackoff, maxBackoff, factor)
			fmt.Fprintf(os.Stderr, "MCP server request failed (attempt %d/%d), retrying in %v: %v\n",
				attempt+1, maxAttempts+1, backoff, err)
			time.Sleep(backoff)
		}
	}

	return nil, fmt.Errorf("MCP server request failed after %d retries: %w", maxAttempts+1, lastErr)
}

// callMCPServer constructs an MCP JSON-RPC request and sends it to the local
// MCP server via HTTP with retry logic.
//
// Binary: 0xb72e00 - cmd.callMCPServer
// Source: cmd/cmd_code_sign.go
//
// Flow:
//  1. Build MCPRequestWithParams: jsonrpc="2.0", method, id, params={tool_name, arguments}
//  2. json.Marshal the request struct
//  3. If marshal error: return fmt.Errorf("failed to marshal request: %w", err)
//  4. Construct URL: fmt.Sprintf("http://127.0.0.1:%d/mcp", config.Port)
//  5. Create http.Client with Transport{ResponseHeaderTimeout: 10s}
//  6. Call doAttemptsWithRetry with:
//     - context.Background()
//     - maxAttempts = 2
//     - baseBackoff = 100ms (0x5F5E100 ns)
//     - maxBackoff  = 2s   (0x77359400 ns)
//     - factor      = 2.0  (0x4000000000000000)
//  7. Return result bytes and error
func callMCPServer(config *CodeSignConfig, jsonrpc string, id string, method string, toolName string, arguments map[string]interface{}) ([]byte, error) {
	// Build the MCP request
	request := MCPRequestWithParams{
		JSONRPC: jsonrpc,
		Method:  method,
		ID:      id,
		Params: &MCPToolCallParams{
			ToolName:  toolName,
			Arguments: arguments,
		},
	}

	// Marshal to JSON
	body, err := json.Marshal(&request)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	// Construct URL: http://127.0.0.1:<port>/mcp
	url := fmt.Sprintf("http://127.0.0.1:%d/mcp", config.Port)

	// Create HTTP client with transport timeout
	transport := &http.Transport{
		ResponseHeaderTimeout: 10 * time.Second, // 0x2540BE400 ns = 10s
	}
	httpClient := &http.Client{
		Transport: transport,
	}

	// Call with retries using context.Background()
	return doAttemptsWithRetry(
		context.Background(),
		url,
		body,
		config.Token,
		httpClient,
		2,                    // maxAttempts
		100*time.Millisecond, // baseBackoff (0x5F5E100 ns)
		2*time.Second,        // maxBackoff (0x77359400 ns)
		2.0,                  // factor (0x4000000000000000 IEEE 754)
	)
}
