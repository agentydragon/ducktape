package util

import (
	"context"
	"fmt"
	"log/slog"
	"os/exec"
)

// Reconstructed from symbol: internal/util.UpdateRemoteTrackingBranch
//
// UpdateRemoteTrackingBranch updates a remote tracking reference to point to the
// specified commit hash by running `git update-ref`. It constructs a ref of the
// form `refs/remotes/<remoteName>/<branchName>` and sets it to commitHash.
func UpdateRemoteTrackingBranch(ctx context.Context, dir string, commitHash string, remoteName string, branchName string) error {
	refspec := fmt.Sprintf("refs/remotes/%s/%s", remoteName, branchName)

	cmd := exec.CommandContext(ctx, "git", "update-ref", refspec, commitHash)
	cmd.Dir = dir

	output, err := cmd.CombinedOutput()
	if err != nil {
		slog.Warn("Failed to update remote tracking branch",
			slog.String("tracking_ref", refspec),
			slog.String("branch", branchName),
			slog.String("revision", commitHash),
			slog.String("error", err.Error()),
			slog.String("output", string(output)),
		)
		return fmt.Errorf("failed to update remote tracking branch %s: %w", refspec, err)
	}

	slog.Debug("Updated remote tracking branch",
		slog.String("tracking_ref", refspec),
		slog.String("branch", branchName),
		slog.String("remote", remoteName),
	)

	return nil
}
