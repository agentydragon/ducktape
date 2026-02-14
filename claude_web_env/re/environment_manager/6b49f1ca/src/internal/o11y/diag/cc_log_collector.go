// Reconstructed from binary 6b49f1ca
// Source: internal/o11y/diag/cc_log_collector.go
//
// This file implements the Claude Code log collector which tails the
// Claude Code log file (/tmp/claude-code.log), parses JSON log entries,
// and buffers them for periodic flushing.

package diag

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/api"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
)

// ccLogCollector tails the Claude Code log file, parses each line
// as JSON, and stores parsed entries in a mutex-protected buffer.
//
// Binary: newCCLogCollector at 0x8330a0
// Binary: (*ccLogCollector).Drain at 0x8332c0
// Binary: (*ccLogCollector).Stop at 0x833460
// Binary: (*ccLogCollector).collect at 0x8334c0
type ccLogCollector struct {
	mu      sync.Mutex
	tailer  *util.Tailer
	entries []api.DiagLogEntry
	wg      sync.WaitGroup
}

// newCCLogCollector creates a new ccLogCollector that tails the given
// Claude Code log file. It starts a background goroutine (collect) to
// read and parse lines.
//
// Binary address: 0x8330a0
// Parameters: ctx context.Context, logPath string, logFilePath string, lineCh <-chan string
func newCCLogCollector(ctx context.Context, logPath string) (*ccLogCollector, error) {
	tailer, err := util.NewTailer(logPath, 0)
	if err != nil {
		return nil, fmt.Errorf("failed to create tailer for claude-code log: %w", err)
	}

	if err := tailer.Start(ctx); err != nil {
		return nil, fmt.Errorf("failed to create tailer for claude-code log: %w", err)
	}

	c := &ccLogCollector{
		tailer: tailer,
	}

	c.wg.Add(1)
	go c.collect()

	return c, nil
}

// Drain locks the collector, swaps out the buffered entries, and returns them.
// This is safe to call concurrently.
//
// Binary address: 0x8332c0
func (c *ccLogCollector) Drain() ([]api.DiagLogEntry, int, int) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if len(c.entries) > 0 {
		entries := c.entries
		c.entries = nil
		return entries, len(entries), 0
	}
	return nil, 0, 0
}

// Stop stops the tailer and waits for the collect goroutine to finish.
//
// Binary address: 0x833460
func (c *ccLogCollector) Stop() {
	c.tailer.Stop()
	c.wg.Wait()
}

// collect is the background goroutine that reads lines from the tailer's
// channel, parses each line as JSON into a map, extracts and parses
// the "timestamp" field, adds a "source" field set to "claude", and
// appends the entry to the buffer.
//
// Binary address: 0x8334c0
// Source: cc_log_collector.go
func (c *ccLogCollector) collect() {
	defer c.wg.Done()

	lineCh := c.tailer.Lines()

	for line := range lineCh {
		if line.Err != nil || line.Text == "" {
			continue
		}

		var parsed map[string]interface{}
		if err := json.Unmarshal([]byte(line.Text), &parsed); err != nil {
			slog.Warn("failed to parse claude-code log line as JSON",
				"error", err,
				"line", line.Text,
			)
			continue
		}

		// Parse or create timestamp
		now := time.Now()
		ts := now

		if tsStr, ok := parsed["timestamp"]; ok {
			if s, ok := tsStr.(string); ok && s != "" {
				// Try parsing RFC3339 format: "2006-01-02T15:04:05.999999999Z07:00"
				if parsed, err := time.Parse("2006-01-02T15:04:05.999999999Z07:00", s); err == nil {
					ts = parsed
				}
			}
		}
		delete(parsed, "timestamp")

		// Set source to "claude"
		parsed["source"] = "claude"

		entry := api.DiagLogEntry{
			Timestamp: ts,
			Fields:    parsed,
		}

		c.mu.Lock()
		c.entries = append(c.entries, entry)
		c.mu.Unlock()
	}
}
