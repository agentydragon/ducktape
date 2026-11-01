package centralsampler

import (
	"strings"
	"time"
)

// EnvironmentContext is the model-visible environment baseline that later updates diff against.
// Codex rust-v0.162.1 core/src/context/world_state/environment.rs EnvironmentsSnapshot, for the
// single configured environment a central Session binds, independently of its reachability.
// Network, filesystem, permission
// profile and subagents sections are omitted: Agentplane owns those boundaries.
// TODO: Add `<timezone>` once the sampler has a configured or executor-reported timezone.
type EnvironmentContext struct {
	Cwd         string  `json:"cwd"`
	Shell       *string `json:"shell,omitempty"`
	CurrentDate string  `json:"current_date"`
}

func (c EnvironmentContext) equal(other EnvironmentContext) bool {
	sameShell := c.Shell == nil && other.Shell == nil || c.Shell != nil && other.Shell != nil && *c.Shell == *other.Shell
	return c.Cwd == other.Cwd && c.CurrentDate == other.CurrentDate && sameShell
}

// currentEnvironmentContext mirrors Codex's local date rendering (`chrono::Local`, `%Y-%m-%d`)
// from core/src/session/world_state.rs Session::build_world_state_for_step.
func currentEnvironmentContext(cwd string, shell *string, now time.Time) EnvironmentContext {
	return EnvironmentContext{Cwd: cwd, Shell: shell, CurrentDate: now.Local().Format("2006-01-02")}
}

// environmentContextUpdate renders the `<environment_context>` fragment that advances previous to
// current, or "" when nothing model-visible changed. A nil previous renders the full context.
// Codex rust-v0.162.1 core/src/context/world_state/environment.rs EnvironmentsState::render_diff
// and RenderedEnvironments::body, single-environment ("legacy single") shape.
func environmentContextUpdate(previous *EnvironmentContext, current EnvironmentContext) string {
	environmentChanged := previous == nil || previous.Cwd != current.Cwd || shellChanged(previous.Shell, current.Shell)
	if !environmentChanged && previous.CurrentDate == current.CurrentDate {
		return ""
	}
	var rendered strings.Builder
	rendered.WriteString("<environment_context>\n")
	if environmentChanged {
		pushEnvironmentElement(&rendered, "cwd", current.Cwd)
		if current.Shell != nil {
			pushEnvironmentElement(&rendered, "shell", *current.Shell)
		}
	}
	pushEnvironmentElement(&rendered, "current_date", current.CurrentDate)
	rendered.WriteString("</environment_context>")
	return rendered.String()
}

// shellChanged follows Codex's EnvironmentSnapshot::has_same_diff_value for two known shells.
// **Deviation:** a shell first learned after the baseline also counts as a change. Codex resolves
// the environment (and its shell) before the first render; the sampler connects its executor
// lazily, so the shell can arrive after the initial context.
func shellChanged(previous, current *string) bool {
	if current == nil {
		return false
	}
	return previous == nil || *previous != *current
}

func pushEnvironmentElement(rendered *strings.Builder, name, value string) {
	rendered.WriteString("  <" + name + ">")
	rendered.WriteString(xmlEscapedText(value))
	rendered.WriteString("</" + name + ">\n")
}

// Codex rust-v0.162.1 core/src/context/environment_context.rs push_xml_escaped_text.
var xmlTextEscaper = strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", `"`, "&quot;", "'", "&apos;")

func xmlEscapedText(value string) string { return xmlTextEscaper.Replace(value) }
