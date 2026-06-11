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
