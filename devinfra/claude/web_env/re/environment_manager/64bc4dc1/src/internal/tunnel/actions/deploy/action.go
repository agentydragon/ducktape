// Reconstructed from binary: environment-manager (Build ID 64bc4dc1)
// Source: internal/tunnel/actions/deploy/action.go
// Module: github.com/anthropics/anthropic/api-go/environment-manager
//
// 64bc4dc1 changes: Vercel and Antspace deploy backends removed.
// New filestore-based deploy mechanism added (FilestoreConfig with
// filestore_url and filesystem_id fields from binary string table).

package deploy

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/tunnel/actions"
)

// DeployAction implements the actions.Action interface for deployments.
// In 64bc4dc1, Vercel Token/TeamID fields are removed; deployment now
// routes through a filestore backend using config.FilesystemConfig.
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
// Reconstructed from old binary (a6f96673) Execute pattern at 0xb99b80,
// adapted for the 64bc4dc1 filestore backend. The old binary's Execute
// flow (lines 84-120) is preserved structurally:
//
//  1. Allocate DeployParams struct (line 90)
//  2. If body is non-empty, json.Unmarshal into params (lines 91-92)
//  3. On unmarshal error: fmt.Errorf("invalid deploy params: %w") (line 93)
//  4. If ProjectDir is empty, set default "/home/claude/project" (lines 96-97)
//  5. ValidateProjectDir (line 100)
//  6. Invoke backend-specific deploy (filestore in 64bc4dc1, was Vercel/Antspace)
//
// The filestore deploy logic itself is fully garble-obfuscated in the 64bc4dc1
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

// ValidateProjectDir validates that the project directory exists and is safe to deploy.
//
// Binary address: 0xba12e0 (old binary a6f96673, symbol preserved)
// The 64bc4dc1 binary retains this function but garble-obfuscates the body.
func ValidateProjectDir(projectDir string) error {
	// Garble-obfuscated in 64bc4dc1. The old binary validates that
	// projectDir exists and is a directory.
	return nil
}

// deployFilestore performs deployment via the filestore backend.
//
// Replaces the old Vercel/Antspace deploy backends. The filestore_url and
// filesystem_id fields from FilesystemConfig configure the upload target.
//
// The full implementation is garble-obfuscated in the 64bc4dc1 binary.
// Known string references from the new binary:
//   - "failed to create deploy request: %w"
//   - "failed to create upload request: %w"
//   - "failed to write tar data for %s: %w"
//   - "failed to write tarball to form: %w"
//   - "failed to read deploy response: %w"
//   - "file upload failed (status %d): %s"
//   - "failed to marshal deploy result: %w"
//   - json:"deploy_url" (DeployResult response field)
//
// The flow likely: creates a tarball of the project dir, uploads it to
// filestore_url with filesystem_id, parses the response for deploy_url,
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

	// Garble-obfuscated: the actual implementation creates a tarball of projectDir,
	// uploads to FilestoreURL with FilesystemID, and parses the deploy_url from
	// the response. See string references above for the error paths.
	_ = ctx
	_ = reporter

	return nil, fmt.Errorf("filestore deploy: garble-obfuscated implementation not reconstructed")
}

// Ensure DeployAction implements actions.Action.
var _ actions.Action = (*DeployAction)(nil)
