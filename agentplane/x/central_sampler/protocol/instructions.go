package protocol

import _ "embed"

// Codex rust-v0.162.1 models-manager gpt-6.1-sol instruction template. Re-copy on pin bumps.
//
//go:embed base_instructions.txt
var BaseInstructions string
