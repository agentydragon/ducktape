// Reconstructed from binary at /tmp/em-re/environment-manager
// Build ID: 6b49f1ca, Go 1.25.6
// Package: internal/sources
// Source: internal/sources/sources.go

package sources

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y/diag"
)

// SourceHandler is the interface that source handlers must implement.
//
// Methods (from itab at 0xf61188):
//   - CanHandle(source config.Source) bool
//   - Process(ctx context.Context, logger *slog.Logger, source config.Source) error
type SourceHandler interface {
	CanHandle(source config.Source) bool
	Process(ctx context.Context, logger *slog.Logger, source config.Source) error
}

// SourceHandlerManager manages source handlers and orchestrates source processing.
//
// Struct layout (from NewSourceHandlerManager field access patterns):
//   offset 0x00: logger *slog.Logger
//   offset 0x08: handlers []SourceHandler (ptr + len + cap at 0x08, 0x10, 0x18)
//   offset 0x20: baseDir string (ptr + len)
//   offset 0x28: sessionID string (ptr + len)
//   offset 0x30: activityRecorder (interface: itab + data at 0x30, 0x38)
//   offset 0x40: isResume bool
type SourceHandlerManager struct {
	logger           *slog.Logger    // offset 0x00
	handlers         []SourceHandler // offset 0x08
	baseDir          string          // offset 0x20
	sessionID        string          // offset 0x28
	activityRecorder interface{}     // offset 0x30 (activity recorder interface)
	isResume         bool            // offset 0x40
}

// NewSourceHandlerManager creates a new SourceHandlerManager and initializes
// the default set of handlers (currently just GitHandler).
//
// Binary address: 0xaf67c0
// Source file: sources.go
//
// Parameters (register-based):
//   AX: logger *slog.Logger
//   BX: baseDir string ptr
//   CX: sessionID string ptr (checked for nil -> error)
//   DI: (git handler param)
//   SI: (git handler param)
//   R8: (git handler param)
//   R9: (git handler param)
//   R10: activityRecorder itab
//   R11: activityRecorder data
func NewSourceHandlerManager(
	logger *slog.Logger,
	baseDir string,
	sessionID string,
	gitProxyManager interface{},
	activityRecorder interface{},
	isResume bool,
) (*SourceHandlerManager, error) {
	if sessionID == "" {
		return nil, fmt.Errorf("base directory cannot be empty")
	}

	mgr := &SourceHandlerManager{
		logger:           logger,
		baseDir:          baseDir,
		sessionID:        sessionID,
		activityRecorder: activityRecorder,
		isResume:         isResume,
	}

	// Create and register the git handler
	gitHandler := NewGitHandler(logger, baseDir, sessionID, gitProxyManager, activityRecorder, isResume)

	// Append GitHandler as a SourceHandler
	mgr.handlers = append(mgr.handlers, gitHandler)

	return mgr, nil
}

// ProcessSources iterates over the provided sources and processes each one
// using the first handler that can handle it. It records metrics via o11y
// and logs progress throughout.
//
// Binary address: 0xaf6a80
// Source file: sources.go
//
// Closure:
//   deferwrap at runtime.deferprocStack - deferred o11y metric recording
func (m *SourceHandlerManager) ProcessSources(
	ctx context.Context,
	logger *slog.Logger,
	sources []config.Source,
) (map[string]interface{}, error) {
	result := make(map[string]interface{})

	if len(sources) == 0 {
		logger.Info("No sources to process")
		result["errors"] = nil
		result["error_message"] = nil
		return result, nil
	}

	// Record o11y metric
	deferredMetric := o11y.RecordFunctionDeferred(logger, ctx, o11y.SourcesProcessingMetric, nil, nil)
	defer deferredMetric()

	startTime := time.Now()

	// Log start
	logger.Info("Starting to process sources",
		"count", len(sources),
	)

	// Create results map and log diagnostic
	sourceCountVal := len(sources)
	diag.LogEnvManagerNoPII(logger, ctx, "sources_processing_started", map[string]interface{}{
		"count": sourceCountVal,
	})

	successCount := 0

	for i, source := range sources {
		sourceStartTime := time.Now()
		sourceIdx := i + 1

		sourceType := source.GetType()

		logger.Info("Processing source",
			"source_index", sourceIdx,
			"source_type", sourceType,
			"total_sources", len(sources),
			"type", sourceType,
		)

		// Find a handler that can handle this source
		var handler SourceHandler
		for _, h := range m.handlers {
			if h.CanHandle(source) {
				handler = h
				break
			}
		}

		if handler == nil {
			msg := fmt.Sprintf("Unable to handle source of type: %s", sourceType)
			logger.Warn(msg)
			logger.Info("No handler found for source type, skipping")
			continue
		}

		// Process the source
		err := handler.Process(ctx, logger, source)
		if err != nil {
			elapsed := time.Since(sourceStartTime)
			errMsg := fmt.Sprintf("Failed to process source type %s: %v", sourceType, err)
			logger.Error("Failed to process source",
				"source_type", sourceType,
				"error", err,
				"duration_ms", elapsed.Milliseconds(),
			)

			return result, fmt.Errorf("failed to process source type %s: %w", sourceType, err)
		}

		logger.Info("Source processed successfully",
			"source_type", sourceType,
			"duration_ms", time.Since(sourceStartTime).Milliseconds(),
		)

		successCount++
	}

	elapsed := time.Since(startTime)
	logger.Info("Completed processing all sources",
		"source_count", len(sources),
		"success_count", successCount,
		"duration_ms", elapsed.Milliseconds(),
	)

	diag.LogEnvManagerNoPII(logger, ctx, "sources_processing_completed", map[string]interface{}{
		"source_count":  len(sources),
		"success_count": successCount,
	})

	return result, nil
}

// UpdateRemoteURLs iterates over the provided sources and updates
// git remote URLs for existing repositories.
//
// Binary address: 0xaf7ae0
// Source file: sources.go
func (m *SourceHandlerManager) UpdateRemoteURLs(
	ctx context.Context,
	logger *slog.Logger,
	sources []config.Source,
) (interface{}, error) {
	if len(sources) == 0 {
		logger.Info("No sources to update")
		return nil, nil
	}

	logger.Info("Updating git remote URLs for existing repositories",
		"count", len(sources),
	)

	for i, source := range sources {
		sourceType := source.GetType()

		// Find a GitHandler
		var gitHandler *GitHandler
		for _, h := range m.handlers {
			if gh, ok := h.(*GitHandler); ok {
				gitHandler = gh
				break
			}
		}

		if gitHandler == nil {
			logger.Warn("No git handler found for updating remote URL")
			continue
		}

		// Check if handler supports this source type
		if !gitHandler.CanHandle(source) {
			logger.Info("Handler does not support remote URL updates",
				"source_index", i,
				"source_type", sourceType,
			)
			continue
		}

		err := gitHandler.UpdateRemoteURL(ctx, logger, source)
		if err != nil {
			logger.Error("Failed to update remote URL, continuing",
				"source_index", i,
				"source_type", sourceType,
				"error", err,
			)
			diag.LogEnvManagerNoPII(logger, ctx, "remote_url_update_failed_nonfatal", nil)
			continue
		}

		logger.Info("Remote URL updated successfully",
			"source_index", i,
			"source_type", sourceType,
		)
	}

	return nil, nil
}

// SetupGitProxyAfterSourcesProcessed finds the GitHandler in the handlers
// list and delegates to its SetupGitProxyAfterSourcesProcessed method.
//
// Binary address: 0xaf8320
// Source file: sources.go
func (m *SourceHandlerManager) SetupGitProxyAfterSourcesProcessed(
	ctx context.Context,
	logger *slog.Logger,
	sources []config.Source,
) (interface{}, error) {
	if len(sources) == 0 {
		return nil, nil
	}

	// Find the GitHandler in our handlers list
	for _, h := range m.handlers {
		if gh, ok := h.(*GitHandler); ok {
			gh.SetupGitProxyAfterSourcesProcessed(ctx, logger, sources)
			return nil, nil
		}
	}

	logger.Warn("No git handler found, skipping proxy setup")
	return nil, nil
}
