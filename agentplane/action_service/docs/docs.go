// Package docs owns the agent guides served over HTTP and through the MCP edge.
package docs

import (
	"embed"
	"errors"
	"fmt"
)

// Agents holds the guides served at /llms.txt and /docs/agents/, under agents/.
//
//go:embed agents
var Agents embed.FS

// Name identifies a published guide, independently of its transport.
type Name string

const (
	Index             Name = "index"
	SessionDelegation Name = "session_delegation"
	ActionLifecycle   Name = "action_lifecycle"
	Recovery          Name = "recovery"
	Kubernetes        Name = "kubernetes"
)

// ErrUnknownGuide means the requested name is outside the published guide catalogue.
var ErrUnknownGuide = errors.New("unknown agent guide")

// Guide is one complete Markdown guide from this binary's documentation snapshot.
type Guide struct {
	Name     Name   `json:"name"`
	Markdown string `json:"markdown"`
}

// Read accepts only published names, never an arbitrary path or URL.
func Read(name Name) (Guide, error) {
	filename := string(name) + ".md"
	switch name {
	case Index:
		filename = "llms.txt"
	case SessionDelegation, ActionLifecycle, Recovery, Kubernetes:
	default:
		return Guide{}, fmt.Errorf("%w: %q", ErrUnknownGuide, name)
	}
	content, err := Agents.ReadFile("agents/" + filename)
	if err != nil {
		return Guide{}, fmt.Errorf("read agent guide %q: %w", name, err)
	}
	return Guide{Name: name, Markdown: string(content)}, nil
}
