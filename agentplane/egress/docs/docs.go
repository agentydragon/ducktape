// Package docs holds the agent-facing egress guides bundled into the CLI.
package docs

import "embed"

// Agents holds the CLI's workflow guides under `agents/`.
//
//go:embed agents
var Agents embed.FS
