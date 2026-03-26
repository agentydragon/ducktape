// Reconstructed from binary 64bc4dc1
// Source: internal/o11y/diag/diag_logs.go
//
// This file implements the DiagService which manages diagnostic logging
// for the environment manager. It collects logs from both the env-manager
// and Claude Code, merges and caps them, and periodically flushes to a
// remote endpoint via a LogFlusher.

package diag

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"slices"
	"strconv"
	"sync"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/api"
)

// diagServiceContextKeyType is a private type used as a context key for storing
// the *DiagService in a context.Context. This allows LogEnvManagerNoPII to be
// called with just a context, extracting the service automatically.
//
// Binary: type at rodata, key variable at 0xc69ea0
type diagServiceContextKeyType struct{}

// diagServiceContextKey is the context key used to store/retrieve a *DiagService.
var diagServiceContextKey = diagServiceContextKeyType{}

// WithDiagService returns a new context with the DiagService stored as a value.
// This is used so that LogEnvManagerNoPII can extract the service from any context.
func WithDiagService(ctx context.Context, svc *DiagService) context.Context {
	return context.WithValue(ctx, diagServiceContextKey, svc)
}

// maxLogEntries is the maximum number of log entries to retain in memory.
const maxLogEntries = 5000

// flushInterval is the interval at which logs are flushed to the remote endpoint.
// Binary: 0x12a05f200 nanoseconds = 5 seconds
const flushInterval = 5 * time.Second

// LogFlusher is the interface for flushing diagnostic logs to a remote endpoint.
// Binary itab: go:itab.*SessionIngressLogFlusher,LogFlusher at 0xf5a1e0
type LogFlusher interface {
	Flush(ctx context.Context, sessionID string, logs []api.DiagLogEntry) error
}

// SessionIngressLogFlusher flushes diagnostic logs via the session ingress
// HTTP API client.
//
// Binary: type:.eq at 0x838980
// Binary: (*SessionIngressLogFlusher).Flush at 0x835fc0
type SessionIngressLogFlusher struct {
	Client    *api.HttpSessionIngressClient
	SessionID string
	AuthToken string
}

// Flush sends the diagnostic log entries to the remote endpoint via
// the session ingress client's PostForwardDiagLogs method.
//
// Binary address: 0x835fc0
func (f *SessionIngressLogFlusher) Flush(ctx context.Context, sessionID string, logs []api.DiagLogEntry) error {
	err := f.Client.PostForwardDiagLogs(ctx, f.SessionID, logs)
	if err != nil {
		return fmt.Errorf("Failed to write diag log entry: %w", err)
	}
	return nil
}

// DiagService manages diagnostic logging, collecting env-manager logs
// and Claude Code logs, merging them, and periodically flushing to
// a remote endpoint.
//
// Binary: NewDiagService at 0x833aa0
// Binary: (*DiagService).Shutdown at 0x834240
// Binary: (*DiagService).logEnvManagerNoPII at 0x834b80
// Binary: (*DiagService).flushPeriodically at 0x835360
// Binary: (*DiagService).collectAndMergeLogs at 0x835920
// Binary: (*DiagService).drainEnvManagerLogs at 0x835ac0
// Binary: (*DiagService).flushDiagLogsToRemote at 0x835ee0
type DiagService struct {
	mu                sync.Mutex
	shutdown          bool
	envManagerLogFile *os.File
	ccDiagLogFile     *os.File
	ccLogCollector    *ccLogCollector
	envManagerLogs    []api.DiagLogEntry
	logFlusherCtx     context.Context
	logFlusher        LogFlusher
	stopCh            chan struct{}
	doneCh            chan struct{}
}

// claudeCodeDiagFilePath holds the cached path to the Claude Code diag file.
var claudeCodeDiagFilePath struct {
	mu   sync.Mutex
	done bool
	svc  *DiagService
}

// NewDiagService creates and returns a new DiagService. It creates temp
// diagnostic files for env-manager ("env-manager.log") and Claude Code
// ("claude-code.log"), starts a CC log collector, and kicks off a
// background goroutine for periodic flushing.
//
// Binary address: 0x833aa0
// Source: diag_logs.go
func NewDiagService(ctx context.Context, sessionID string, logFlusherCtx context.Context, logFlusher LogFlusher) (*DiagService, error, error) {
	// Create env-manager diagnostic temp file
	envManagerFile, err := createDiagFile(ctx, sessionID, "env-manager.log")
	if err != nil {
		return nil, fmt.Errorf("failed to create diag log for environment-manager: %w", err), nil
	}

	// Create claude-code diagnostic temp file
	ccFile, err := createDiagFile(ctx, sessionID, "claude-code")
	if err != nil {
		// Close the already-opened env manager file
		if envManagerFile != nil {
			envManagerFile.Close()
		}
		return nil, fmt.Errorf("failed to create diag log for claude-code: %w", err), nil
	}

	// Create CC log collector using the cc diag file's path
	ccLogPath := ccFile.Name()
	ccCollector, err := newCCLogCollector(ctx, ccLogPath)
	if err != nil {
		envManagerFile.Close()
		ccFile.Close()
		return nil, nil, err
	}

	stopCh := make(chan struct{})
	doneCh := make(chan struct{}, 1)

	svc := &DiagService{
		envManagerLogFile: envManagerFile,
		ccDiagLogFile:     ccFile,
		ccLogCollector:    ccCollector,
		logFlusherCtx:     logFlusherCtx,
		logFlusher:        logFlusher,
		stopCh:            stopCh,
		doneCh:            doneCh,
	}

	// Start periodic flushing goroutine
	go svc.flushPeriodically(ctx, sessionID)

	return svc, nil, nil
}

// createDiagFile creates a temporary diagnostic log file with the given
// name prefix. It logs the created file path.
//
// Binary address: 0x833e60
func createDiagFile(ctx context.Context, sessionID string, namePrefix string) (*os.File, error) {
	pattern := fmt.Sprintf("diag-%s-*", namePrefix)
	f, err := os.CreateTemp("", pattern)
	if err != nil {
		return nil, fmt.Errorf("failed to create %s temp file: %w", namePrefix, err)
	}

	slog.Info("Created diagnostic file",
		"path", f.Name(),
		"name", namePrefix,
	)

	return f, nil
}

// GetClaudeCodeDiagFilePath returns the path to the Claude Code diagnostic
// log file, if a DiagService has been created. Uses a sync.Mutex and
// cached lookup.
//
// Binary address: 0x834080
func GetClaudeCodeDiagFilePath(svc *DiagService, sessionID string) (string, string) {
	if svc == nil {
		return "", ""
	}

	claudeCodeDiagFilePath.mu.Lock()
	defer claudeCodeDiagFilePath.mu.Unlock()

	if !claudeCodeDiagFilePath.done {
		if svc.ccDiagLogFile == nil {
			return "", ""
		}
		return svc.ccDiagLogFile.Name(), ""
	}

	return "", ""
}

// Shutdown gracefully shuts down the DiagService. It stops the CC log
// collector, signals the flush goroutine to stop, waits for it, performs
// a final flush, and closes log files.
//
// Binary address: 0x834240
// Source: diag_logs.go
func (d *DiagService) Shutdown(ctx context.Context, sessionID string) {
	d.mu.Lock()
	if d.shutdown {
		d.mu.Unlock()
		return
	}
	d.shutdown = true
	d.mu.Unlock()

	// Stop the CC log collector
	if d.ccLogCollector != nil {
		d.ccLogCollector.Stop()
	}

	// Signal the flush goroutine to stop
	close(d.stopCh)

	// Wait for flush goroutine to finish (with timeout via doneCh select)
	select {
	case <-d.doneCh:
		// Flusher stopped
	case <-ctx.Done():
		slog.Warn("Shutdown timed out waiting for diag log flusher to stop")
	}

	// Final collect and flush
	logs := d.collectAndMergeLogs(nil, nil, nil)
	if err := d.flushDiagLogsToRemote(sessionID, logs); err != nil {
		if !errors.Is(err, api.ErrEndpointNotImplemented) {
			slog.Warn("Failed to shutdown diagnostic logging service",
				"error", err,
			)
		}
	}

	// Close log files
	if d.envManagerLogFile != nil {
		d.envManagerLogFile.Close()
	}
	if d.ccDiagLogFile != nil {
		d.ccDiagLogFile.Close()
	}
}

// LogEnvManagerNoPII is the package-level function for logging env-manager
// diagnostic entries without PII. It extracts a *DiagService from the context
// via ctx.Value and delegates to logEnvManagerNoPII if found.
//
// Binary address: 0x834ac0
// Source file: diag_logs.go
//
// Parameters (register ABI):
//
//	AX+BX: ctx (context.Context interface: itab+data)
//	CX+DI: event string (ptr+len)
//	SI: data (map[string]interface{} pointer, may be nil)
//
// Key behaviors from disassembly:
//   - 0x834ae7: MOVQ 0x30(AX), DX — loads ctx.Value method from itab (4th method)
//   - 0x834aeb-0x834af5: calls ctx.Value(diagServiceKey) with static key
//   - 0x834afe-0x834b08: type-checks returned value against *DiagService
//   - If not *DiagService: returns (no-op)
//   - If *DiagService: delegates to logEnvManagerNoPII with all original params
func LogEnvManagerNoPII(ctxOrLogger interface{}, event string, data map[string]interface{}) {
	// Accept either context.Context or *slog.Logger for compatibility with
	// reconstructed call sites where the binary passes the logger directly.
	ctx, ok := ctxOrLogger.(context.Context)
	if !ok {
		return
	}
	// Binary 0x834ae7-0x834afc: ctx.Value(diagServiceKey)
	val := ctx.Value(diagServiceContextKey)
	svc, ok := val.(*DiagService)
	if !ok || svc == nil {
		return
	}

	// Binary 0x834b26: delegate to logEnvManagerNoPII
	svc.logEnvManagerNoPII(ctx, event, data)
}

// logEnvManagerNoPII creates a DiagLogEntry from the event name and optional
// data map, then appends it to the env-manager log buffer.
//
// Binary address: 0x834b80
// Source file: diag_logs.go
//
// Parameters (register ABI):
//
//	AX: self (*DiagService)
//	BX+CX: ctx (context.Context interface)
//	DI+SI: event string (ptr+len)
//	R8: data (map[string]interface{} pointer)
//
// Key behaviors from disassembly:
//   - 0x834be1-0x834bf5: mutex lock via LOCK CMPXCHGL (offset 0x00)
//   - 0x834c5b: checks d.shutdown (offset 0x38)
//   - 0x834c66: time.Now() for timestamp
//   - 0x834cf6: runtime.makemap_small — creates Fields map
//   - Stores "source" → "env-manager", "message" → event in Fields
//   - If data != nil, merges data entries into Fields
//   - Appends DiagLogEntry to d.envManagerLogs
func (d *DiagService) logEnvManagerNoPII(ctx context.Context, event string, data map[string]interface{}) {
	d.mu.Lock()
	defer d.mu.Unlock()

	if d.shutdown {
		slog.Warn("DiagService is shutdown; discarding log entry")
		return
	}

	entry := api.DiagLogEntry{
		Timestamp: time.Now(),
		Fields: map[string]interface{}{
			"source":  "env-manager",
			"message": event,
		},
	}

	// Merge additional data from the provided map
	for k, v := range data {
		entry.Fields[k] = v
	}

	d.envManagerLogs = append(d.envManagerLogs, entry)
}

// drainEnvManagerLogs locks the service, swaps out the env-manager log
// buffer, and returns the old entries.
//
// Binary address: 0x835ac0
func (d *DiagService) drainEnvManagerLogs() ([]api.DiagLogEntry, int, int) {
	d.mu.Lock()
	defer d.mu.Unlock()

	if len(d.envManagerLogs) > 0 {
		entries := d.envManagerLogs
		d.envManagerLogs = nil
		return entries, len(entries), 0
	}
	return nil, 0, 0
}

// collectAndMergeLogs drains both the env-manager logs and CC log collector,
// appends new entries to the running accumulator, caps the total at
// maxLogEntries, sorts by timestamp, and returns the merged result.
//
// Binary address: 0x835920
func (d *DiagService) collectAndMergeLogs(existing []api.DiagLogEntry, _ interface{}, _ interface{}) []api.DiagLogEntry {
	// Drain env-manager logs
	emLogs, _, _ := d.drainEnvManagerLogs()

	// Drain CC logs
	ccLogs, _, _ := d.ccLogCollector.Drain()

	// Append env-manager logs
	existing = appendAndCapLogs(existing, emLogs)

	// Append CC logs
	existing = appendAndCapLogs(existing, ccLogs)

	// Sort by timestamp
	slices.SortFunc(existing, func(a, b api.DiagLogEntry) int {
		return a.Timestamp.Compare(b.Timestamp)
	})

	return existing
}

// appendAndCapLogs appends new entries to the existing slice and trims
// it to maxLogEntries if exceeded. When trimming, it logs a warning
// with the number of dropped entries.
//
// Binary address: 0x835c60
func appendAndCapLogs(existing []api.DiagLogEntry, new []api.DiagLogEntry) []api.DiagLogEntry {
	existing = append(existing, new...)

	if len(existing) > maxLogEntries {
		dropped := len(existing) - maxLogEntries
		existing = existing[dropped:]
		slog.Warn("Dropping old diagnostic log entries",
			"dropped", dropped,
			"max", maxLogEntries,
		)
	}

	return existing
}

// flushPeriodically is the background goroutine that periodically collects,
// merges, and flushes diagnostic logs to the remote endpoint.
//
// Binary address: 0x835360
func (d *DiagService) flushPeriodically(ctx context.Context, sessionID string) {
	ticker := time.NewTicker(flushInterval)
	defer ticker.Stop()

	var accumulated []api.DiagLogEntry

	for {
		select {
		case <-ticker.C:
			// Collect and merge
			accumulated = d.collectAndMergeLogs(accumulated, nil, nil)

			// Flush to remote
			if err := d.flushDiagLogsToRemote(sessionID, accumulated); err != nil {
				if errors.Is(err, api.ErrEndpointNotImplemented) {
					// Endpoint not yet available, keep accumulating
					continue
				}
				slog.Warn("Failed to flush diagnostic logs",
					"error", err,
				)
				continue
			}

			// Clear accumulated on success
			accumulated = nil

		case <-d.stopCh:
			return
		}
	}
}

// flushDiagLogsToRemote sends the accumulated logs to the remote endpoint
// via the LogFlusher. Returns nil if no logs to flush or no flusher configured.
//
// Binary address: 0x835ee0
func (d *DiagService) flushDiagLogsToRemote(sessionID string, logs []api.DiagLogEntry) error {
	if len(logs) == 0 {
		return nil
	}

	if d.logFlusher == nil {
		return nil
	}

	err := d.logFlusher.Flush(d.logFlusherCtx, sessionID, logs)
	if err != nil {
		return fmt.Errorf("failed to flush diag logs: %w", err)
	}
	return nil
}

// LogsEnabled checks the ENVIRONMENT_RUNNER_ENABLE_DIAG_LOGS environment
// variable and returns true if it is set to a truthy value.
// Truthy: "1", "t", "T", "TRUE", "True", "true"
// Falsy: "0", "f", "F", "FALSE", "False", "false", empty
//
// Binary address: 0x8360a0
// Source: diag_logs.go
func LogsEnabled() bool {
	val := os.Getenv("ENVIRONMENT_RUNNER_ENABLE_DIAG_LOGS")
	if val == "" {
		return false
	}

	b, err := strconv.ParseBool(val)
	if err != nil {
		return false
	}
	return b
}
