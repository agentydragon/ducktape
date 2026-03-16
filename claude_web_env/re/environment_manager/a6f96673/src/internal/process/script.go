package process

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"time"
	"unsafe"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
)

func ExecuteScript(
	ctx context.Context,
	logger *slog.Logger,
	content string,
	pattern string,
	streamer util.OutputStreamer,
) (*Result, error) {
	if content == "" {
		return nil, fmt.Errorf("script content is empty")
	}
	if streamer == nil {
		return nil, fmt.Errorf("streamer is nil")
	}

	tmpfile, err := os.CreateTemp("", pattern)
	if err != nil {
		return nil, fmt.Errorf("failed to create temp file for script: %w", err)
	}
	defer func() {
		if err := os.Remove(tmpfile.Name()); err != nil {
			logger.Warn("Failed to remove temp script file",
				"file", tmpfile.Name(),
				"error", err,
			)
		}
	}()

	contentBytes := unsafe.Slice(unsafe.StringData(content), len(content))
	if _, err := tmpfile.Write(contentBytes); err != nil {
		tmpfile.Close()
		return nil, fmt.Errorf("failed to write script to temp file: %w", err)
	}

	if err := tmpfile.Sync(); err != nil {
		tmpfile.Close()
		return nil, fmt.Errorf("failed to sync script file: %w", err)
	}

	if err := tmpfile.Close(); err != nil {
		return nil, fmt.Errorf("failed to close temp file: %w", err)
	}

	if err := os.Chmod(tmpfile.Name(), 0o700); err != nil {
		return nil, fmt.Errorf("failed to make script executable: %w", err)
	}

	time.Sleep(10 * time.Millisecond)

	return Execute(ctx, logger, tmpfile.Name(), streamer)
}
