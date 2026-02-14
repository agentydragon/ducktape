package process

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os/exec"
	"sync"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
)

// Result holds the outcome of a process execution.
type Result struct {
	ExitCode  int
	StartTime time.Time
	EndTime   time.Time
	Duration  time.Duration
	Error     error
}

var bufferPool = sync.Pool{
	New: func() any {
		buf := make([]byte, 32768)
		return &buf
	},
}

func Execute(
	ctx context.Context,
	logger *slog.Logger,
	path string,
	streamer util.OutputStreamer,
) (*Result, error) {
	if path == "" {
		return nil, fmt.Errorf("path is empty")
	}
	if streamer == nil {
		return nil, fmt.Errorf("streamer is nil")
	}

	startTime := time.Now()

	result := &Result{
		StartTime: startTime,
	}

	cmd := exec.CommandContext(ctx, path)

	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("failed to create stdout pipe: %w", err)
	}

	stderrPipe, err := cmd.StderrPipe()
	if err != nil {
		return nil, fmt.Errorf("failed to create stderr pipe: %w", err)
	}

	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("failed to start command: %w", err)
	}

	var wg sync.WaitGroup
	wg.Add(2)

	go func() {
		defer wg.Done()
		streamPipe(ctx, logger, stdoutPipe, util.StreamStdout, streamer)
	}()

	go func() {
		defer wg.Done()
		streamPipe(ctx, logger, stderrPipe, util.StreamStderr, streamer)
	}()

	wg.Wait()

	err = cmd.Wait()

	endTime := time.Now()
	result.EndTime = endTime
	result.Duration = endTime.Sub(startTime)

	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			result.ExitCode = exitErr.ExitCode()
			result.Error = fmt.Errorf("process exited with code %d", result.ExitCode)
		} else {
			result.ExitCode = -1
			result.Error = fmt.Errorf("process failed: %w", err)
		}
	} else {
		result.ExitCode = 0
	}

	return result, nil
}

func streamPipe(
	ctx context.Context,
	logger *slog.Logger,
	pipe io.ReadCloser,
	streamType util.StreamType,
	streamer util.OutputStreamer,
) {
	defer func() {
		if err := pipe.Close(); err != nil {
			pipeName := streamName(streamType)
			logger.Warn("Failed to close pipe",
				"stream", pipeName,
				"error", err,
			)
		}
	}()

	bufPtr := bufferPool.Get().(*[]byte)
	defer bufferPool.Put(bufPtr)
	buffer := *bufPtr

	for {
		n, err := pipe.Read(buffer)
		if n > 0 {
			if writeErr := streamer(ctx, streamType, buffer[:n]); writeErr != nil {
				pipeName := streamName(streamType)
				logger.Debug("Streamer requested stop",
					"stream", pipeName,
				)
				return
			}
		}
		if err != nil {
			if err != io.EOF {
				pipeName := streamName(streamType)
				logger.Warn("Error reading from pipe",
					"stream", pipeName,
					"error", err,
				)
			}
			return
		}
		if n == 0 {
			pipeName := streamName(streamType)
			logger.Warn("Unexpected zero read without error",
				"stream", pipeName,
			)
			return
		}
	}
}

func streamName(streamType util.StreamType) string {
	switch streamType {
	case util.StreamStdout:
		return "stdout"
	case util.StreamStderr:
		return "stderr"
	default:
		return "unknown"
	}
}
