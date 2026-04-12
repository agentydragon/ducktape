// Package shared holds embedded content used by both the anthropic and byoc
// environment types: the default Claude Code settings JSON and the stop hook
// script. Both packages copy these in their init() functions.
//
// Reconstructed from a6f96673 DWARF extraction, carried forward to 495ea204.
// Source path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/envtype/shared/
//
// Key symbols:
//   - shared.DefaultSettingsJSON (0x15adda0)
//   - shared.StopHookScript      (0x15addb0)
//
// TODO(re): Actual byte content is not recoverable from the garble-obfuscated
// 495ea204 binary. The variables are declared here to match the package
// structure; their values are nil until recovered via runtime observation.
package shared

// DefaultSettingsJSON is the default Claude Code settings JSON written to
// .claude/settings.json during environment initialization.
// Shared by both the anthropic and byoc environment types.
var DefaultSettingsJSON []byte

// StopHookScript is the stop hook shell script written to the hooks directory
// during environment initialization (mode 0755).
// Shared by both the anthropic and byoc environment types.
var StopHookScript []byte
