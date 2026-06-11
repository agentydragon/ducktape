// Package envtype defines the EnvironmentType interface and related types for
// environment lifecycle management. Each environment type (anthropic, byoc)
// implements this interface to handle initialization, authentication, and
// session configuration.
//
// Reconstructed from a6f96673 DWARF extraction, carried forward to 495ea204.
// Source: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/envtype/
//
// Evidence:
//   - itab: *anthropic.anthropicEnvironmentType → envtype.EnvironmentType
//   - itab: *byoc.byocEnvironmentType → envtype.EnvironmentType
//   - Method set derived from common methods on both implementations.
package envtype

import (
	"context"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
)

// EnvironmentType defines the interface for environment lifecycle management.
// Implementations include anthropic.anthropicEnvironmentType and byoc.byocEnvironmentType.
//
// Interface reconstructed from itab entries:
//
//	go:itab.*anthropic.anthropicEnvironmentType,envtype.EnvironmentType (0xf60ac0)
//	go:itab.*byoc.byocEnvironmentType,envtype.EnvironmentType (0xf60ae8)
//
// Method set derived from common methods across both implementing types.
type EnvironmentType interface {
	// SetStartupContext configures the startup context for the environment.
	// anthropic: 0xaf8620, byoc: 0xb04660
	SetStartupContext(ctx *config.StartupContext)

	// SetAuthContext provides authentication context to the environment.
	// anthropic: 0xaf8680, byoc: 0xb046c0
	SetAuthContext(authCtx interface{})

	// SetSessionMode sets the session mode for the environment.
	// anthropic: 0xaf86e0, byoc: 0xb04600
	SetSessionMode(mode config.SessionMode)

	// Initialize performs the full environment initialization sequence.
	// This includes cloning repos, installing languages, running init scripts, etc.
	// anthropic: 0xaf8740, byoc: 0xb04740
	Initialize(ctx context.Context) error

	// GetCWD returns the current working directory for the environment.
	// anthropic: 0xafe8e0, byoc: 0xb04720
	GetCWD() string

	// GetClaudeEnvironmentVariables returns environment variables to pass to Claude Code.
	// anthropic: 0xafe900, byoc: 0xb05ae0
	GetClaudeEnvironmentVariables() map[string]string

	// CreateLeaseManager creates a lease manager for the environment.
	// Only byoc implements this with real logic (0xb07cc0);
	// anthropic may not need one or returns nil.
	CreateLeaseManager(ctx context.Context, sessionID string, workID string, apiBaseURL string) (LeaseManager, error)
}

// LeaseManager manages environment leases (keep-alive, renewal).
// Referenced by byoc.(*byocEnvironmentType).CreateLeaseManager.
// The actual implementation lives in a separate package (e.g., internal/lease).
type LeaseManager interface {
	Start(ctx context.Context) error
	Stop()
}

// Factory is a function type that creates an EnvironmentType from a logger and raw config.
// Used in the registration system to instantiate environment types by name.
//
// Evidence: string "Factory" in binary; anthropic.New and byoc.New signatures match.
type Factory func(logger interface{}, rawConfig interface{}) (EnvironmentType, error)

// Registration holds the metadata for a registered environment type.
// Each environment sub-package (anthropic, byoc) exports a Registration variable
// that is set during init().
//
// Evidence:
//   - anthropic.Registration (0x1589458)
//   - byoc.Registration (0x1589460)
type Registration struct {
	// Name is the environment type identifier (e.g., "anthropic", "byoc").
	Name string
	// New creates a new instance of this environment type.
	New Factory
}
