// Reconstructed from environment-manager binary (Build ID: 6b49f1ca)
// Source: internal/tunnel/actions/deploy/action.go
// Module: github.com/anthropics/anthropic/api-go/environment-manager

package deploy

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os/exec"
	"strings"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/tunnel/actions"
)

// DeployAction implements the actions.Action interface for Vercel deployments.
// It handles deploying project files to Vercel, including npm install,
// file collection, upload, deployment creation, and readiness polling.
//
// Implements: actions.Action
type DeployAction struct {
	Token  string // offset 0x08 (ptr + len)
	TeamID string // offset 0x18 (ptr + len)
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

// Execute runs the deploy action. Steps:
// 1. Unmarshal JSON request body into DeployParams
// 2. Default project dir to "/home/project/" if empty (0x14 = 20 chars at address)
// 3. Validate the project directory (must be under /home/project/)
// 4. Create a VercelClient with token, teamID, 60s timeout, and API base URL
// 5. Derive project name using ProjectName()
// 6. Log "deploy starting" with action name and project dir
// 7. Send progress "installing dependencies"
// 8. Run `npm install --production` via os/exec.CommandContext, truncate output to 10240 bytes on error
// 9. Send progress "collecting files"
// 10. Call CollectFiles
// 11. Upload files to Vercel
// 12. Call CreateDeployment
// 13. Call WaitForReady
// 14. Return DeployURL result
//
// Binary address: 0xb3db80
func (a *DeployAction) Execute(
	ctx context.Context,
	path string,
	body []byte,
	reporter actions.ProgressReporter,
) (*actions.ActionResult, error) {
	// Step 1: Unmarshal request body
	params := &DeployParams{}
	if len(body) > 0 {
		if err := json.Unmarshal(body, params); err != nil {
			return nil, fmt.Errorf("invalid deploy params: %w", err)
		}
	}

	// Step 2: Default project directory
	if params.ProjectDir == "" {
		params.ProjectDir = "/home/project/" // 0x14 = 20 chars (with padding: "/home/project/\x00...")
	}

	// Step 3: Validate project directory
	if err := ValidateProjectDir(params.ProjectDir); err != nil {
		return nil, err
	}

	// Step 4: Create Vercel client
	client := &VercelClient{
		Token:   a.Token,
		TeamID:  a.TeamID,
		Timeout: 60 * time.Second, // 0xdf8475800 ns
		BaseURL: "https://api.vercel.com", // 0x16 = 22 chars
	}

	// Step 5: Derive project name
	projectName := ProjectName(path)

	slog.Info("deploy starting",
		"action", a.Name(),
		"project_dir", params.ProjectDir,
		"project_name", projectName,
		"path", path,
	)

	// Step 6: Report progress - send initial progress
	if err := reporter.SendProgress("installing_dependencies", "Installing dependencies...", 0.0); err != nil {
		// Log but continue
		slog.Warn("failed to send progress", "error", err)
	}

	// Step 7: Run npm install --production
	npmCmd := exec.CommandContext(ctx, "npm", "install", "--production")
	npmCmd.Dir = params.ProjectDir
	npmOutput, err := npmCmd.CombinedOutput()
	if err != nil {
		// Truncate output to 10240 bytes (0x2800) if too large
		if len(npmOutput) > 10240 {
			npmOutput = npmOutput[:10240]
		}
		return nil, fmt.Errorf("npm install failed: %s: %w", string(npmOutput), err)
	}

	// Step 8: Report progress - collecting files
	if err := reporter.SendProgress("collecting_files", "Collecting files...", 0.2); err != nil {
		slog.Warn("failed to send progress", "error", err)
	}

	// Step 9: Collect files
	files, err := CollectFiles(params.ProjectDir)
	if err != nil {
		return nil, err
	}

	// Step 10: Report progress - uploading files
	if err := reporter.SendProgress("uploading_files", "Uploading files...", 0.4); err != nil {
		slog.Warn("failed to send progress", "error", err)
	}

	// Step 11: Upload files
	for _, f := range files {
		if err := client.UploadFile(ctx, f); err != nil {
			return nil, fmt.Errorf("failed to upload file %s: %w", f.Path, err)
		}
	}

	// Step 12: Report progress - creating deployment
	if err := reporter.SendProgress("deploying", "Creating deployment...", 0.7); err != nil {
		slog.Warn("failed to send progress", "error", err)
	}

	// Get commit SHA for git source
	commitSHA := getCommitSHA(ctx, params.ProjectDir)

	// Step 13: Create deployment
	deployment, err := client.CreateDeployment(ctx, projectName, files, commitSHA)
	if err != nil {
		return nil, err
	}

	// Step 14: Report progress - waiting for ready
	if err := reporter.SendProgress("waiting_for_ready", "Waiting for deployment...", 0.9); err != nil {
		slog.Warn("failed to send progress", "error", err)
	}

	// Step 15: Wait for deployment to be ready
	deployURL, err := client.WaitForReady(ctx, deployment)
	if err != nil {
		return nil, err
	}

	slog.Info("deploy complete",
		"deploy_url", deployURL,
		"project_name", projectName,
	)

	return &actions.ActionResult{
		Data: &DeployResult{
			DeployURL: deployURL,
		},
	}, nil
}

// getCommitSHA retrieves the current git HEAD commit SHA by running
// `git rev-parse HEAD` in the given directory. Returns an empty string
// if the command fails.
//
// Binary address: 0xb3f120
func getCommitSHA(ctx context.Context, dir string) string {
	cmd := exec.CommandContext(ctx, "git", "rev-parse", "HEAD")
	cmd.Dir = dir
	output, err := cmd.Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(output))
}

// Ensure DeployAction implements actions.Action.
var _ actions.Action = (*DeployAction)(nil)
