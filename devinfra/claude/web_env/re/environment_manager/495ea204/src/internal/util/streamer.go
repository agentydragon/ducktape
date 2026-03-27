// Reconstructed from binary: environment-manager (Build ID 495ea204)
// Source: internal/util/streamer.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/internal/util
//
// These types are referenced by process.streamPipe and process.Execute but were
// not defined in any existing util source file. Reconstructed from:
//   - process.streamPipe disassembly: StreamType is an integer compared against
//     0 ("stdout", 6 chars) and 1 ("stderr", 6 chars), default "unknown" (7 chars).
//   - process.Execute disassembly: OutputStreamer is passed as an interface pair
//     (itab + data) and called with (ctx, StreamType, []byte) returning error.
//   - noopStopper itab: go:itab.*session.noopStopper,util.Stopper (b71486df) confirms Stopper
//     is an interface with a single Stop() method.
//     (a6f96673 had go:itab.*cmd.noopStopper,util.Stopper; moved in b71486df)

package util

import "context"

// StreamType identifies the output stream (stdout or stderr).
//
// Reconstructed from process.streamPipe (0xae45e0):
//
//	CMP StreamType, $0x0 → "stdout" (6 chars)
//	CMP StreamType, $0x1 → "stderr" (6 chars)
//	default → "unknown" (7 chars)
type StreamType int

const (
	// StreamStdout represents the standard output stream.
	StreamStdout StreamType = 0

	// StreamStderr represents the standard error stream.
	StreamStderr StreamType = 1
)

// OutputStreamer is a callback function type that receives output from a
// running process. It is called with chunks of bytes from stdout or stderr.
// Returning a non-nil error signals that streaming should stop.
//
// Reconstructed from process.Execute (0xae3d60) and process.streamPipe (0xae45e0):
//   - Execute passes OutputStreamer as a parameter to streamPipe
//   - streamPipe calls it via indirect CALL R10 with (ctx, StreamType, []byte)
//   - Return value (AX) is checked for non-nil to stop streaming
type OutputStreamer func(ctx context.Context, streamType StreamType, data []byte) error

// Stopper is an interface for stopping a long-running component.
//
// Reconstructed from itab entries:
//
//	go:itab.*session.noopStopper,util.Stopper (b71486df, new location)
//	go:itab.*util.PeriodicInvoker,util.Stopper (0xf5b480)
//
// The noopStopper.Stop method is a single RET instruction,
// confirming the interface has only a Stop() method with no parameters
// or return values.
type Stopper interface {
	Stop()
}

// ClaudeCodeVersion holds the installed Claude Code version string.
// Binary: util.ClaudeCodeVersion (global at 0x161f320 in a6f96673).
// Set in cmd.AddTaskRunCommand.func1 at cmd_task_run.go:345 (0xbb5af1),
// which stores the version string returned by claude.InstallOrUpdateClaudeCode
// (cmd_task_run.go:337). The write uses a GC write barrier (gcWriteBarrier2
// at 0xbb5b20) to update both the string pointer and length fields.
//
// Read sites (all read-only via MOVQ):
//   - api/client.go:156 — API client user-agent header
//   - api/session_ingress_client.go:81 — session ingress client header
//   - api/work_client.go:62 — work client header
//   - mcp/sign_operations.go:102 — code signing version field
//   - process/poller.go:159 — stuck-process detection threshold
//   - process/whoami.go:65 — whoami response
//   - claude/handler.go:298 — Claude handler version reporting
//   - envtype/byoc/byoc.go:573 — BYOC environment version
var ClaudeCodeVersion string
