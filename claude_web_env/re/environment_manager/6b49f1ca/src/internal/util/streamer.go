// Reconstructed from binary: environment-manager (Build ID 6b49f1ca)
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
//     (Old binary 6b49f1ca had go:itab.*cmd.noopStopper,util.Stopper)

package util

import "context"

// StreamType identifies the output stream (stdout or stderr).
//
// Reconstructed from process.streamPipe (0xae45e0):
//   CMP StreamType, $0x0 → "stdout" (6 chars)
//   CMP StreamType, $0x1 → "stderr" (6 chars)
//   default → "unknown" (7 chars)
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
//   go:itab.*session.noopStopper,util.Stopper (b71486df, new location)
//   go:itab.*util.PeriodicInvoker,util.Stopper (0xf5b480)
//
// The noopStopper.Stop method is a single RET instruction,
// confirming the interface has only a Stop() method with no parameters
// or return values.
type Stopper interface {
	Stop()
}

// ClaudeCodeVersion holds the installed Claude Code version string.
// Binary: util.ClaudeCodeVersion (new BSS global in b71486df).
// Set during Claude Code installation (claude package or setup command).
// Used for version reporting and stuck-process detection thresholds.
// TODO(re): exact write site not recovered; likely set in claude.InstallOrUpdateClaudeCode.
var ClaudeCodeVersion string
