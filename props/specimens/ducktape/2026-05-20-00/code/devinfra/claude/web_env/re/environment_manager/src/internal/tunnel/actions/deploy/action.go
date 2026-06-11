// Reconstructed from binary: environment-manager (Build ID 495ea204)
// Source: internal/tunnel/actions/deploy/action.go
// Module: github.com/anthropics/anthropic/api-go/environment-manager
//
// Filestore-based deploy mechanism (FilestoreConfig with
// filestore_url and filesystem_id fields from binary string table).

package deploy

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/tunnel/actions"
)

// DeployAction implements the actions.Action interface for deployments.
// Deployment routes through a filestore backend using config.FilesystemConfig.
//
// Implements: actions.Action
type DeployAction struct {
	FilesystemConfig *config.FilesystemConfig
}

// DeployParams holds the parameters for a deploy action request.
type DeployParams struct {
	ProjectDir string `json:"project_dir"`
}

// DeployResult holds the result of a successful deployment.
type DeployResult struct {
	DeployURL string `json:"deploy_url"`
}

// Name returns the action name "deploy".
//
// Binary address: 0xb3db40
func (a *DeployAction) Name() string {
	return "deploy" // 6 bytes
}

// Timeout returns the action timeout of 3 minutes.
//
// Binary address: 0xb3db60
func (a *DeployAction) Timeout() time.Duration {
	return 3 * time.Minute // 0x29e8d60800 = 180,000,000,000 ns
}

// Execute runs the deploy action via the filestore backend.
//
// Binary address: 0xb3db80
//
// Execute flow:
//  1. Allocate DeployParams struct
//  2. If body is non-empty, json.Unmarshal into params
//  3. On unmarshal error: fmt.Errorf("invalid deploy params: %w")
//  4. If ProjectDir is empty, set default "/home/claude/project"
//  5. ValidateProjectDir
//  6. Invoke filestore deploy
//
// The filestore deploy logic itself is fully garble-obfuscated in the 495ea204
// binary. The deployFilestore stub below captures the known interface contract
// and config field usage from binary string references.
func (a *DeployAction) Execute(
	ctx context.Context,
	path string,
	body []byte,
	reporter actions.ProgressReporter,
) (*actions.ActionResult, error) {
	var params DeployParams

	if len(body) > 0 {
		if err := json.Unmarshal(body, &params); err != nil {
			return nil, fmt.Errorf("invalid deploy params: %w", err)
		}
	}

	if params.ProjectDir == "" {
		params.ProjectDir = "/home/claude/project"
	}

	if err := ValidateProjectDir(params.ProjectDir); err != nil {
		return nil, err
	}

	return a.deployFilestore(ctx, params.ProjectDir, reporter)
}

// ValidateProjectDir validates that the project directory exists and is a directory.
//
// Binary address: 0xba12e0 (old binary a6f96673, symbol preserved)
func ValidateProjectDir(projectDir string) error {
	info, err := os.Stat(projectDir)
	if err != nil {
		return fmt.Errorf("project directory does not exist: %w", err)
	}
	if !info.IsDir() {
		return fmt.Errorf("project path is not a directory: %s", projectDir)
	}
	return nil
}

// deployFilestore performs deployment via the filestore backend.
// The filestore_url and filesystem_id fields from FilesystemConfig
// configure the upload target.
//
// Reconstructed from garble-obfuscated binary 495ea204.
// Known string references from the binary:
//   - "failed to create deploy request: %w"
//   - "failed to create upload request: %w"
//   - "failed to write tar data for %s: %w"
//   - "failed to write tarball to form: %w"
//   - "failed to read deploy response: %w"
//   - "file upload failed (status %d): %s"
//   - "failed to marshal deploy result: %w"
//   - json:"deploy_url" (DeployResult response field)
//
// Flow: creates a tarball of the project dir, uploads it via multipart form
// to filestore_url with filesystem_id, parses the response for deploy_url,
// and returns it as an ActionResult.
func (a *DeployAction) deployFilestore(
	ctx context.Context,
	projectDir string,
	reporter actions.ProgressReporter,
) (*actions.ActionResult, error) {
	if a.FilesystemConfig == nil {
		return nil, fmt.Errorf("filestore config is required for deploy action")
	}

	slog.Info("deploy_filestore_starting",
		"filestore_url", a.FilesystemConfig.FilestoreURL,
		"filesystem_id", a.FilesystemConfig.FilesystemID,
		"project_dir", projectDir,
	)

	// Create tarball of project directory into a buffer.
	var tarBuf bytes.Buffer
	tw := tar.NewWriter(&tarBuf)

	err := filepath.Walk(projectDir, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}

		relPath, err := filepath.Rel(projectDir, path)
		if err != nil {
			return err
		}

		header, err := tar.FileInfoHeader(info, "")
		if err != nil {
			return err
		}
		header.Name = relPath

		if err := tw.WriteHeader(header); err != nil {
			return fmt.Errorf("failed to write tar data for %s: %w", relPath, err)
		}

		if info.IsDir() {
			return nil
		}

		f, err := os.Open(path)
		if err != nil {
			return fmt.Errorf("failed to write tar data for %s: %w", relPath, err)
		}
		defer f.Close()

		if _, err := io.Copy(tw, f); err != nil {
			return fmt.Errorf("failed to write tar data for %s: %w", relPath, err)
		}

		return nil
	})
	if err != nil {
		return nil, err
	}
	if err := tw.Close(); err != nil {
		return nil, fmt.Errorf("failed to write tar data for close: %w", err)
	}

	// Build multipart upload request with tarball and filesystem_id.
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)

	if err := writer.WriteField("filesystem_id", a.FilesystemConfig.FilesystemID); err != nil {
		return nil, fmt.Errorf("failed to create upload request: %w", err)
	}

	part, err := writer.CreateFormFile("tarball", "project.tar")
	if err != nil {
		return nil, fmt.Errorf("failed to create upload request: %w", err)
	}

	if _, err := io.Copy(part, &tarBuf); err != nil {
		return nil, fmt.Errorf("failed to write tarball to form: %w", err)
	}

	if err := writer.Close(); err != nil {
		return nil, fmt.Errorf("failed to write tarball to form: %w", err)
	}

	// Create and send the deploy request.
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, a.FilesystemConfig.FilestoreURL, &body)
	if err != nil {
		return nil, fmt.Errorf("failed to create deploy request: %w", err)
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to create deploy request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read deploy response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("file upload failed (status %d): %s", resp.StatusCode, string(respBody))
	}

	// Parse deploy_url from response.
	var result DeployResult
	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("failed to read deploy response: %w", err)
	}

	resultJSON, err := json.Marshal(result)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal deploy result: %w", err)
	}

	slog.Info("deploy_filestore_complete",
		"deploy_url", result.DeployURL,
	)

	var resultData interface{}
	if err := json.Unmarshal(resultJSON, &resultData); err != nil {
		return nil, fmt.Errorf("failed to marshal deploy result: %w", err)
	}

	return &actions.ActionResult{Data: resultData}, nil
}

// Ensure DeployAction implements actions.Action.
var _ actions.Action = (*DeployAction)(nil)
