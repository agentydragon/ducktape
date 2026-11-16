package environmentcontext

import "testing"

// The expected strings are Codex rust-v0.162.1's rendered `<environment_context>` shape, which the
// model is trained on.
func TestUpdate(t *testing.T) {
	bash, zsh := "bash", "zsh"
	baseline := &Context{Cwd: "/fixture", Shell: &bash, CurrentDate: "2026-01-02"}
	for name, test := range map[string]struct {
		previous *Context
		current  Context
		want     string
	}{
		"first render is complete": {
			current: Context{Cwd: "/fixture", Shell: &bash, CurrentDate: "2026-01-02"},
			want:    "<environment_context>\n  <cwd>/fixture</cwd>\n  <shell>bash</shell>\n  <current_date>2026-01-02</current_date>\n</environment_context>",
		},
		"unchanged renders nothing": {
			previous: baseline,
			current:  Context{Cwd: "/fixture", Shell: &bash, CurrentDate: "2026-01-02"},
		},
		"date rollover renders only the date": {
			previous: baseline,
			current:  Context{Cwd: "/fixture", Shell: &bash, CurrentDate: "2026-01-03"},
			want:     "<environment_context>\n  <current_date>2026-01-03</current_date>\n</environment_context>",
		},
		"changed shell renders the environment": {
			previous: baseline,
			current:  Context{Cwd: "/fixture", Shell: &zsh, CurrentDate: "2026-01-02"},
			want:     "<environment_context>\n  <cwd>/fixture</cwd>\n  <shell>zsh</shell>\n  <current_date>2026-01-02</current_date>\n</environment_context>",
		},
		"first learned shell renders the environment": {
			previous: &Context{Cwd: "/fixture", CurrentDate: "2026-01-02"},
			current:  Context{Cwd: "/fixture", Shell: &bash, CurrentDate: "2026-01-02"},
			want:     "<environment_context>\n  <cwd>/fixture</cwd>\n  <shell>bash</shell>\n  <current_date>2026-01-02</current_date>\n</environment_context>",
		},
		"lost shell renders nothing": {
			previous: baseline,
			current:  Context{Cwd: "/fixture", CurrentDate: "2026-01-02"},
		},
		"values are XML-escaped": {
			current: Context{Cwd: `/a&b<c>"d'`, CurrentDate: "2026-01-02"},
			want:    "<environment_context>\n  <cwd>/a&amp;b&lt;c&gt;&quot;d&apos;</cwd>\n  <current_date>2026-01-02</current_date>\n</environment_context>",
		},
	} {
		t.Run(name, func(t *testing.T) {
			if got := Update(test.previous, test.current); got != test.want {
				t.Fatalf("update=%q, want %q", got, test.want)
			}
		})
	}
}
