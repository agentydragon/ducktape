// Package docs holds the agent-facing documentation embedded in the Notification CLI.
package docs

import "embed"

// Agents holds the documentation embedded by `agentplane notifications guide`.
//
//go:embed agents
var Agents embed.FS
