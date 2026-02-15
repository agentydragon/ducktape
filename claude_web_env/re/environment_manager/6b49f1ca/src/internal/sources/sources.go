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
// Struct layout (from NewSourceHandlerManager field stores at 0xaf6872-0xaf68ea):
//
//	offset 0x00: logger *slog.Logger (from AX, stored via DX at 0xaf6872)
//	offset 0x08: handlers []SourceHandler (ptr at 0x08, len at 0x10, cap at 0x18)
//	              — initialized to static empty slice, populated by append after NewGitHandler
//	offset 0x20: baseDir string (ptr at 0x20 from BX, len at 0x28 from CX)
//	offset 0x30: activityRecorder interface{} (itab at 0x30 from R10, data at 0x38 from R11)
//	offset 0x40: isResume bool (from stack at 0x90(SP))
//
// NOTE: sessionID, gitProxyConfig, outcomes, and processMode are NOT stored on this struct.
// They are passed through to NewGitHandler only.
type SourceHandlerManager struct {
	logger           *slog.Logger    // offset 0x00
	handlers         []SourceHandler // offset 0x08
	baseDir          string          // offset 0x20
	activityRecorder interface{}     // offset 0x30 (activity recorder interface)
	isResume         bool            // offset 0x40
}

// NewSourceHandlerManager creates a new SourceHandlerManager and initializes
// the default set of handlers (currently just GitHandler).
//
// Binary address: 0xaf67c0
// Source file: sources.go
//
// Parameters (register ABI):
//
//	AX: logger *slog.Logger
//	BX+CX: baseDir string (ptr+len)
//	DI+SI: sessionID string (ptr+len) — CX checked for nil at 0xaf67f2 → error
//	R8: gitProxyConfig (pointer) — passed through to NewGitHandler
//	R9: outcomes map[string][]string — passed through to NewGitHandler
//	R10+R11: activityRecorder interface{} — stored in struct AND passed to NewGitHandler
//	stack[0]+stack[1]: processMode string — passed through to NewGitHandler
//	stack[2]: isResume bool — stored in struct AND passed to NewGitHandler
//
// Key behaviors from disassembly:
//   - 0xaf67f2: TESTQ CX, CX — nil check on baseDir.len (Go register order: BX=ptr, CX=len)
//   - 0xaf6843-0xaf684a: runtime.newobject for SourceHandlerManager
//   - 0xaf6872-0xaf68ea: store fields into struct (logger, baseDir, activityRecorder, isResume)
//   - 0xaf690c-0xaf6932: pass all params through to NewGitHandler
//   - 0xaf6937-0xaf69d1: append returned *GitHandler to handlers slice as SourceHandler
//   - 0xaf69d4-0xaf69dd: return (mgr, nil, nil) — success
//   - 0xaf69de-0xaf6a03: error path: fmt.Errorf("base directory cannot be empty")
func NewSourceHandlerManager(
	logger *slog.Logger,
	baseDir string,
	sessionID string,
	gitProxyConfig interface{},
	outcomes map[string][]string,
	activityRecorder interface{},
	processMode string,
	isResume bool,
) (*SourceHandlerManager, error) {
	// Binary 0xaf67f2: TESTQ CX,CX — nil check (baseDir.len register)
	if baseDir == "" {
		return nil, fmt.Errorf("base directory cannot be empty")
	}

	// Binary 0xaf6843-0xaf68ea: allocate and populate struct
	mgr := &SourceHandlerManager{
		logger:           logger,
		baseDir:          baseDir,
		activityRecorder: activityRecorder,
		isResume:         isResume,
	}

	// Binary 0xaf690c-0xaf6932: call NewGitHandler with all params passed through
	// AX=logger, BX+CX=baseDir, DI+SI=sessionID, R8=gitProxyConfig, R9=outcomes,
	// R10+R11=activityRecorder, stack=processMode+isResume
	gitHandler := NewGitHandler(logger, baseDir, sessionID, gitProxyConfig, outcomes, activityRecorder, processMode, isResume)

	// Binary 0xaf6937-0xaf69d1: append gitHandler to handlers slice
	// with SourceHandler itab at go:itab.*GitHandler,SourceHandler (0xaf69a4)
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

	startTime := time.Now()

	// Binary 0xaf6af4-0xaf6b1b: o11y.RecordFunctionDeferred(ctx, SourcesProcessingMetric, ...)
	deferredMetric := o11y.RecordFunctionDeferred("sources_processing", nil, nil, startTime, nil)
	defer deferredMetric(nil, nil)

	// Log start
	logger.Info("Starting to process sources",
		"count", len(sources),
	)

	// Create results map and log diagnostic
	sourceCountVal := len(sources)
	diag.LogEnvManagerNoPII(ctx, "sources_processing_started", map[string]interface{}{
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

	diag.LogEnvManagerNoPII(ctx, "sources_processing_completed", map[string]interface{}{
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
			diag.LogEnvManagerNoPII(ctx, "remote_url_update_failed_nonfatal", nil)
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
