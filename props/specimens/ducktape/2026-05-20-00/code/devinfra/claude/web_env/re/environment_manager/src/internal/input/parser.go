// Reconstructed from a6f96673 DWARF extraction, carried forward to 495ea204.
// Source: internal/input/parser.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/input/parser.go
//
// This file defines the InputParser interface implemented by V0Parser and V1Parser.
// The file exists in the old binary's DWARF paths but contains only type definitions
// (no TEXT symbols — all methods are in v0_parser.go and v1_parser.go).
//
// Interface itabs:
//   - go:itab.*V0Parser,InputParser (0xfac6c0)
//   - go:itab.*V1Parser,InputParser (0xfac6e0)

package input

import "context"

// InputParser is the interface for parsing input data into a ParsedContext.
// V0Parser and V1Parser implement this interface for their respective input
// format versions.
//
// The interface has a single Parse method that takes a context and raw input
// bytes, and returns a ParsedContext with all parsed session configuration.
type InputParser interface {
	Parse(ctx context.Context, data []byte) (*ParsedContext, error)
}
