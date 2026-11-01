// Package runtimeconfig owns the deployment-published runtime facts shared by
// the app and existing-environment discovery.
package runtimeconfig

import "strings"

type Provenance string

const ProvenanceDeploymentConfig Provenance = "DEPLOYMENT_CONFIG"

// ModelCatalog is the deployment's configured model and harness offer set.
type ModelCatalog struct {
	Models    []ModelOption       `mapstructure:"models" json:"models"`
	Harnesses map[string][]string `mapstructure:"harnesses" json:"harnesses"`
}

// NormalizeHarnessNames restores canonical harness identifiers after generic map decoders fold
// their keys. The discovery and app APIs expose the uppercase enum names configured by cdk8s.
func (c *ModelCatalog) NormalizeHarnessNames() {
	normalized := make(map[string][]string, len(c.Harnesses))
	for harness, models := range c.Harnesses {
		normalized[strings.ToUpper(harness)] = models
	}
	c.Harnesses = normalized
}

type ModelOption struct {
	Model            string   `mapstructure:"model" json:"model"`
	DisplayName      string   `mapstructure:"display_name" json:"display_name"`
	ReasoningEfforts []string `mapstructure:"reasoning_efforts" json:"reasoning_efforts"`
}

// TemplateRuntimeConfig applies configured offers and path guidance only to
// the named SandboxTemplate. A missing path is unavailable guidance.
type TemplateRuntimeConfig struct {
	WorkspaceRoot         string     `mapstructure:"workspace_root" json:"workspace_root"`
	WorkspaceProvenance   Provenance `mapstructure:"workspace_provenance" json:"workspace_provenance"`
	WorkspaceRootEnforced bool       `mapstructure:"workspace_root_enforced" json:"workspace_root_enforced"`
	CreatesMissingCWD     bool       `mapstructure:"creates_missing_cwd" json:"creates_missing_cwd"`
}

// Catalog is emitted by deployment config and read by the app and discovery
// service; it is not loaded from the app database.
type Catalog struct {
	Models    ModelCatalog                     `mapstructure:"models" json:"models"`
	Templates map[string]TemplateRuntimeConfig `mapstructure:"templates" json:"templates"`
}
