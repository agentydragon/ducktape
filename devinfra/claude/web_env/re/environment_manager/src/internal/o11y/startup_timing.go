// Reconstructed from binary: Build ID 0b86a2a0 (release-1186d93b9-ext)
// Source: the startup-timing telemetry contract.
//
// PLACEMENT NOTE: in the original tree these types live in the Claude Code
// launcher package (garbled `TaVHwGAw` / type package `fkWASaBCp`) and the
// environment-init package (garbled `l2uwXm6g2pDF` / type package
// `jWMEREkG76`), not in internal/o11y. They are documented here because they
// are purely telemetry payloads and because this RE task owns the observability
// surface. Do not read the package name as recovered fact.
//
// All struct definitions below were read out of the binary's Go runtime type
// metadata (abi.StructType walked from moduledata.types = 0x25e3020); field
// names are garble-randomised, json tags and offsets are the binary's.

package o11y

import "encoding/json"

// ---------------------------------------------------------------------------
// Claude Code stream-json envelope
// ---------------------------------------------------------------------------

// ClaudeStreamMessage is the subset of Claude Code's `--output-format
// stream-json` envelope that the launcher parses. One struct covers both the
// `{"type":"system","subtype":"init"}` banner (StartupTiming, MCPServers,
// Tools, Skills, ClaudeCodeVersion) and the `{"type":"result"}` terminator
// (TTFTMs, TimeToRequestMs, TimeToRequestFromSpawnMs, NumTurns).
//
// Binary: fkWASaBCp.msioI3Z3HLWs, vaddr 0x28d6e00, size 0xc0, 11 fields.
// ENTIRELY NEW: the previous binary has no struct carrying `startup_timing`,
// `skills`, `mcp_servers`, `claude_code_version`, `ttft_ms`,
// `time_to_request_ms` or `time_to_request_from_spawn_ms`.
//
// Parsed by TaVHwGAw.(*beIJgiC_lO).d52ZXCUGT at 0x21b12e0. That function
// json.Unmarshals a line into this struct (the type descriptor 0x28d6e00 is
// loaded at 0x21b131d) and then dispatches on the discriminators — the
// comparisons are inline immediates, so they are directly readable:
//
//	0x21b1382  cmpl $0x74737973 ("syst") ; 0x21b138f cmpw $0x6d65 ("em")
//	0x21b139c  len==4 ; 0x21b13ab cmpl $0x74696e69 ("init")
//	0x21b16b9  cmpl $0x75736572 ("resu") ; 0x21b16c6 cmpw $0x746c ("lt")
//	0x21b16d3  test ttft_ms pointer (offset +0xa0) for nil
//
// i.e. exactly two handled messages: `system`/`init` and `result`.
type ClaudeStreamMessage struct {
	Type                     string            `json:"type"`                          // +0x00
	Subtype                  string            `json:"subtype"`                       // +0x10
	StartupTiming            StartupTiming     `json:"startup_timing"`                // +0x20
	MCPServers               []json.RawMessage `json:"mcp_servers"`                   // +0x48
	Tools                    []json.RawMessage `json:"tools"`                         // +0x60
	Skills                   []json.RawMessage `json:"skills"`                        // +0x78
	ClaudeCodeVersion        string            `json:"claude_code_version"`           // +0x90
	TTFTMs                   *float64          `json:"ttft_ms"`                       // +0xa0
	TimeToRequestMs          *float64          `json:"time_to_request_ms"`            // +0xa8
	TimeToRequestFromSpawnMs *float64          `json:"time_to_request_from_spawn_ms"` // +0xb0
	NumTurns                 int               `json:"num_turns"`                     // +0xb8
}

// StartupTiming is Claude Code's own start-up profile, reported to the
// environment manager inside the `system`/`init` banner.
//
// Binary: fkWASaBCp.sUKA0lk9oycE, vaddr 0x28831a0, size 0x28, 4 fields.
//
// `Phases` holds per-phase durations and `PhaseStartMs` per-phase start offsets,
// both keyed by phase name and both relative to `TimeOriginMs`.
//
// PHASE ENUMERATION — NOT RECOVERABLE FROM THIS BINARY.
// Both fields are `map[string]float64` decoded generically by encoding/json.
// The struct type 0x28831a0 has exactly one code reference (the unmarshal in
// d52ZXCUGT, via its parent type), and there is no literal-keyed lookup into
// either map anywhere in .text — the environment manager never names a phase.
// The phase vocabulary is therefore defined by the *producer* (the Claude Code
// CLI), and cannot be enumerated from environment-manager.
//
// TODO(re): enumerate the phase names from the Claude Code CLI bundle instead,
// or from a captured `system`/`init` line on a live session.
type StartupTiming struct {
	Entrypoint   string             `json:"entrypoint"`     // +0x00
	Phases       map[string]float64 `json:"phases"`         // +0x10
	TimeOriginMs float64            `json:"time_origin_ms"` // +0x18
	PhaseStartMs map[string]float64 `json:"phase_start_ms"` // +0x20
}

// ClaudeProcessContext is the {cwd, env, argv} snapshot of the Claude Code
// process, recorded by the warm-spare path.
//
// Binary: fkWASaBCp.emhN9JV378TT, vaddr 0x27e7120, size 0x30, 3 fields. NEW.
// Referenced from TaVHwGAw.(*Qx7xZhVlaq46).Claim at 0x21ab7c0 (type descriptor
// loaded at 0x21ab896 and 0x21ab8ad) — i.e. it is the spare's recorded launch
// context, compared/adopted when a task claims a pre-booted spare.
//
// TODO(re): Claim's body is not fully disassembled, so whether this context is
// *matched against* the claiming task or merely *reported* is not established.
type ClaudeProcessContext struct {
	Cwd  string            `json:"cwd"`  // +0x00
	Env  map[string]string `json:"env"`  // +0x10
	Argv []string          `json:"argv"` // +0x18
}

// ---------------------------------------------------------------------------
// Environment-init stage status
// ---------------------------------------------------------------------------

// StageStatus is the pass/fail summary of environment initialisation.
//
// Binary: jWMEREkG76.UBwWVvZZku7, vaddr 0x27e80e0, size 0x28, 3 fields. NEW —
// the previous binary has no struct with `ok` / `failed_stage` /
// `failure_category`.
//
// Produced by l2uwXm6g2pDF.(*W6O3FbYja2cf).Run at 0x245e1e0:
//
//	0x246db73  movb $0x1, 0xea0(%rsp)   ; Ok = true
//	0x246db8b  two stack strings copied into +0x08 and +0x18
//	0x246dbbb  runtime.convT(type 0x27e80e0, &status)
//	0x246dbd9  encoding/json.Marshal(status)
//
// so the record is serialised to JSON on the success path; the failure path
// (Run.func3 at 0x24737c0) builds the same struct with Ok=false.
//
// FAILED_STAGE / FAILURE_CATEGORY VALUES — NOT DETERMINED.
// Every candidate value is a garble `-literals` encrypted function-local string
// reconstructed inline (see the byte-wise XOR/ADD/SUB ladder at 0x246dc3f
// onwards, keyed off runtime globals 0x3b4d438 / 0x3b4d4c8). They are absent
// from a process core taken at `--help` exit because the enclosing function
// never runs, and the enclosing subcommands must not be executed for real.
//
// TODO(re): recover the value sets either by emulating the inline decryptors
// (unicorn) or by breaking on 0x245e1e0 in a live session and dumping the
// marshalled JSON.
type StageStatus struct {
	Ok              bool   `json:"ok"`                         // +0x00
	FailedStage     string `json:"failed_stage,omitempty"`     // +0x08
	FailureCategory string `json:"failure_category,omitempty"` // +0x18
}

// InitStage is one recorded initialisation stage.
//
// Binary: jWMEREkG76.gTlI6xxfZYVO, vaddr 0x289c520, size 0x58, 5 fields; held
// as a slice at jWMEREkG76.W6O3FbYja2cf+0xa0. The struct carries no json tags,
// so it is internal bookkeeping, but it is the population from which
// StageStatus.FailedStage is drawn.
//
// Field types are from RTTI; the NAMES below are guesses (marked) because the
// struct has no tags and the producing code was not disassembled.
type InitStage struct {
	Name      string                 // +0x00 // GUESS: stage name
	Category  string                 // +0x10 // GUESS: failure category bucket
	StartedAt any                    // +0x20 // time.Time (cAKtTFMwQ1J.Vddj0etYQ2h)
	EndedAt   any                    // +0x38 // time.Time
	Fields    map[string]interface{} // +0x50
}

// TODO(re): InitStage.StartedAt/EndedAt are `any` only because importing
// time here would misrepresent the (unrecovered) field names as facts; the
// RTTI type at both offsets is the 24-byte struct that every other time.Time
// in this binary uses.
