// Reconstructed from a6f96673 DWARF extraction, carried forward to 64bc4dc1.
// Source: internal/envtype/anthropic/config.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/envtype/anthropic/config.go
//
// Key symbols:
//   - anthropic.DecodeConfig (0xb1dda0)
//
// This file contains the Anthropic environment configuration types and
// the DecodeConfig function for deserializing environment configuration JSON.

package anthropic

import (
	"encoding/json"
	"fmt"
)

// AnthropicConfig holds the Anthropic environment configuration decoded from
// the environment JSON payload. The struct is allocated via runtime.newobject
// at line 29, and its fields are populated by json.Unmarshal at line 30.
//
// Struct layout recovered from:
//   - DecodeConfig (0xb1dda0): validates CWD (offset 0x28 len != 0)
//   - GetCWD (0xb17ca0): returns config[0x20:0x30] = CWD string
//   - Initialize (0xb11720): accesses config[0x08] (logger), config[0x10] (sources),
//     config[0x20:0x30] (CWD), config[0x38] (init_script or pointer field)
//   - JSON tags from new binary strings: "cwd", "init_script,omitempty",
//     "languages,omitempty", "sources"
//
// Layout (offsets from runtime.newobject type descriptor):
//
//	0x00: field0 (16 bytes, string — likely a name or identifier)
//	0x10: field1 (16 bytes, pointer/interface — used for sources slice)
//	0x20: CWD (16 bytes, string — validated non-empty in DecodeConfig)
//	0x30: field3 (8+ bytes, pointer — checked for nil at 0x38 in Initialize)
type AnthropicConfig struct {
	// Fields before CWD occupy 32 bytes (offsets 0x00-0x1F).
	// From JSON tags and Initialize usage, these likely include sources config.
	Sources    json.RawMessage `json:"sources,omitempty"`
	Languages  json.RawMessage `json:"languages,omitempty"`
	CWD        string          `json:"cwd"`
	InitScript *string         `json:"init_script,omitempty"`
}

// DecodeConfig unmarshals a JSON byte slice into an AnthropicConfig.
// Returns the config and an error if unmarshaling fails or if required
// fields are missing.
//
// Binary address: 0xb1dda0
// Source lines: 28-39
//
// Assembly flow:
//  1. runtime.newobject(AnthropicConfig) at line 29
//  2. json.Unmarshal(data, &config) at line 30
//  3. On unmarshal error: fmt.Errorf("failed to decode anthropic config: %w") at line 31
//     (format string is 40 = 0x28 bytes)
//  4. Check config field at offset 0x28 is non-empty at line 35
//  5. If empty: fmt.Errorf("anthropic config: cwd field is required") at line 36
//     (format string is 50 = 0x32 bytes)
//  6. On success: return config, nil at line 39
func DecodeConfig(data []byte) (*AnthropicConfig, error) {
	config := &AnthropicConfig{}

	if err := json.Unmarshal(data, config); err != nil {
		return nil, fmt.Errorf("failed to decode anthropic config: %w", err)
	}

	if config.CWD == "" {
		return nil, fmt.Errorf("anthropic config: cwd field is required")
	}

	return config, nil
}
