// Reconstructed from binary 495ea204
// Source: internal/o11y/metrics.go
//
// This file defines the global metric counter/gauge variables and
// convenience functions for incrementing specific metrics.

package o11y

import "context"

// ---- Global metric variables (data segment addresses) ----

// EnvManagerStartCounter tracks environment manager start events.
// Binary address: 0x1589468
var EnvManagerStartCounter *O11yMetric

// EnvManagerEndCounter tracks environment manager end events.
// Binary address: 0x1589470
var EnvManagerEndCounter *O11yMetric

// ClaudeCodeStartCounter tracks Claude Code start events.
// Binary address: 0x1589478
var ClaudeCodeStartCounter *O11yMetric

// ClaudeCodeEndCounter tracks Claude Code end events.
// Binary address: 0x1589480
var ClaudeCodeEndCounter *O11yMetric

// GitCheckoutMetric tracks git checkout operations.
// Binary address: 0x1589488
var GitCheckoutMetric *O11yMetric

// OrchestratorPollAttemptCounter tracks orchestrator poll attempts.
// Binary address: 0x1589490
var OrchestratorPollAttemptCounter *O11yMetric

// OrchestratorPollErrorCounter tracks orchestrator poll errors.
// Binary address: 0x1589498
var OrchestratorPollErrorCounter *O11yMetric

// OrchestratorPollDurationMetric tracks orchestrator poll duration.
// Binary address: 0x15894a0
var OrchestratorPollDurationMetric *O11yMetric

// OrchestratorQueueEmptyCounter tracks empty orchestrator queue events.
// Binary address: 0x15894a8
var OrchestratorQueueEmptyCounter *O11yMetric

// OrchestratorSessionStartCounter tracks orchestrator session starts.
// Binary address: 0x15894b0
var OrchestratorSessionStartCounter *O11yMetric

// OrchestratorSessionEndCounter tracks orchestrator session ends.
// Binary address: 0x15894b8
var OrchestratorSessionEndCounter *O11yMetric

// OrchestratorTimeoutCounter tracks orchestrator timeouts.
// Binary address: 0x15894c0
var OrchestratorTimeoutCounter *O11yMetric

// StartupTotalMetric tracks total startup duration.
// Binary address: 0x15894c8
var StartupTotalMetric *O11yMetric

// ClaudeInstallMetric tracks Claude Code installation duration.
// Binary address: 0x15894d0
var ClaudeInstallMetric *O11yMetric

// EnvInitMetric tracks environment initialization duration.
// Binary address: 0x15894d8
var EnvInitMetric *O11yMetric

// MCPRegistrationMetric tracks MCP registration duration.
// Binary address: 0x15894e0
var MCPRegistrationMetric *O11yMetric

// PluginMarketplaceMetric tracks plugin marketplace setup duration.
// Binary address: 0x15894e8
var PluginMarketplaceMetric *O11yMetric

// SourcesProcessingMetric tracks sources processing duration.
// Binary address: 0x15894f0
var SourcesProcessingMetric *O11yMetric

// GitProxySetupMetric tracks git proxy setup duration.
// Binary address: 0x15894f8
var GitProxySetupMetric *O11yMetric

// LanguageSetupMetric tracks language setup duration.
// Binary address: 0x1589500
var LanguageSetupMetric *O11yMetric

// InitScriptMetric tracks init script execution duration.
// Binary address: 0x1589508
var InitScriptMetric *O11yMetric

// ---- Metrics added in Build ID 0b86a2a0 (release-1186d93b9-ext) ----
//
// These six metric names appear in Build ID 0b86a2a0's decrypted literal set
// and in none of the previous binary's. The names and descriptions quoted below
// are exact strings recovered from a process core of the running binary (garble
// `-literals` encrypts them in .rodata, so `strings` on the file does not show
// them). The Go variable names are invented; their data-segment addresses were
// not re-derived for this build.
//
//	claude_code.spawn_to_ready.latency_ms
//	  "Wall-clock from CLI launch setup (tailer init, docker wrap,
//	   spawn/spare-claim) to first system/init stdout line"
//
//	claude_code.spawn_to_ready.outcome
//	  "Count of CLI spawn outcomes (ready, exited_before_ready,
//	   no_init_observed, unobservable)"
//
//	claude_code.spare.claim_miss
//	  "Cold spawn instead of warm-spare claim, attributed by reason"
//
//	claude_code.spare.spawn_to_claim_window_ms
//	  "Wall-clock from spare spawn/adopt to Claim (the overlap window W)"
//
//	claude_code.binary_prewarm.duration
//	  "Time to read the claude binary into guest page cache
//	   (CCR_PREWARM_CC_BINARY)"
//
//	claude_code.gateway_arg.dropped
//	  "Count of ClaudeCodeArgs entries dropped by buildArgsFromGatewayConfig"

// SpawnToReadyLatencyMetric records wall-clock from launch setup to the first
// `system`/`init` line on Claude Code's stdout.
//
// TODO(re): data-segment address not derived for Build ID 0b86a2a0.
var SpawnToReadyLatencyMetric *O11yFunctionMetric

// SpawnToReadyOutcomeCounter counts CLI spawn outcomes. The tag key is
// literally "outcome": a plaintext .rodata string at 0x2914857 (len 7) loaded
// by FKPKJ5B0zZ.NwaNmS at 0x200b773. The four documented values are "ready",
// "exited_before_ready", "no_init_observed" and "unobservable"; "ready" is
// confirmed independently as a plaintext 5-byte literal at 0x29107eb passed to
// NwaNmS from the `system`/`init` handler at 0x21b13df.
var SpawnToReadyOutcomeCounter *O11yMetric

// SpareClaimMissCounter counts cold spawns that could not claim a warm spare.
var SpareClaimMissCounter *O11yMetric

// SpareSpawnToClaimWindowMetric records the spare spawn-to-claim overlap window.
var SpareSpawnToClaimWindowMetric *O11yFunctionMetric

// BinaryPrewarmDurationMetric records the CCR_PREWARM_CC_BINARY page-cache warm.
var BinaryPrewarmDurationMetric *O11yFunctionMetric

// GatewayArgDroppedCounter counts ClaudeCodeArgs entries dropped by the
// sandbox-gateway argument builder.
var GatewayArgDroppedCounter *O11yMetric

// recordSpawnToReady emits the spawn_to_ready pair.
//
// Binary address: 0x200b720 (FKPKJ5B0zZ.NwaNmS). Register ABI at entry:
// AX/BX carry a two-word value (a time.Time-shaped pair, per the caller at
// 0x21b13d1-0x21b13d5), CX/DI the outcome string (ptr, len), SIL a bool. The
// bool selects between the plaintext literals "true" (0x290fa1a, len 4) and
// "false" (0x291053e, len 5) at 0x200b79f-0x200b7c3, i.e. it becomes a
// stringified tag value. The function ends by tail-calling FKPKJ5B0zZ.CdqIOIL
// at 0x2013d60.
//
// TODO(re): the tag KEY for that boolean is a garble-encrypted literal and was
// not recovered ("warm_spare"/"adopted" are plausible but unproven), so the
// parameter is named neutrally here.
// TODO(re): the latency leg (SpawnToReadyLatencyMetric) is emitted inside
// FKPKJ5B0zZ.CdqIOIL (0x2013d60), which was not disassembled.
func recordSpawnToReady(elapsedMs float64, outcome string, boolTagValue bool) {
	tags := map[string]string{"outcome": outcome}
	tags["TODO_re_unrecovered_tag_key"] = "false"
	if boolTagValue {
		tags["TODO_re_unrecovered_tag_key"] = "true"
	}
	Increment(context.Background(), SpawnToReadyOutcomeCounter, []TagProvider{&kvTagProvider{tags: tags}})
	_ = elapsedMs
}

// IncrementEnvManagerEnd increments the EnvManagerEndCounter with the
// given exit code tag and error tags.
//
// Binary address: 0xa51b00
// Parameters: name, tags (code string, exitReason string, err error, errItf interface{})
func IncrementEnvManagerEnd(name string, tags []string, exitReason string, code string, exitReasonVal string, err error, errItf interface{}) {
	codeTags := make(map[string]string)
	codeTags["code"] = exitReason
	codeProvider := &kvTagProvider{tags: codeTags}

	errorProvider, _ := ErrorTags(err, errItf)

	Increment(context.Background(), EnvManagerEndCounter, []TagProvider{codeProvider, errorProvider})
}

// IncrementClaudeCodeEnd increments the ClaudeCodeEndCounter with
// error tags extracted from the given error.
//
// Binary address: 0xa51c80
func IncrementClaudeCodeEnd(name string, tags []string, err error, errItf interface{}) {
	errorProvider, _ := ErrorTags(err, errItf)
	Increment(context.Background(), ClaudeCodeEndCounter, []TagProvider{errorProvider})
}

// IncrementOrchestratorSessionEnd increments the OrchestratorSessionEndCounter
// with a reason tag and error tags.
//
// Binary address: 0xa51d20
func IncrementOrchestratorSessionEnd(ctx context.Context, err error) {
	errorProvider, _ := ErrorTags(err, nil)

	Increment(ctx, OrchestratorSessionEndCounter, []TagProvider{errorProvider})
}

// RecordDuration records a duration metric with the given value and tags.
//
// Binary address: 0xa51dc0
func RecordDuration(name string, tags []string, metric *O11yMetric, durationMs float64, providers ...TagProvider) {
	svc, _ := GetO11yService(name, tags)
	merged := mergeTags(providers)
	_ = merged
	// Records the duration via svc.RecordGauge
	_ = svc
	_ = durationMs
}
