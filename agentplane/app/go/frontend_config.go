package main

import (
	"fmt"
	"net/url"
)

// FrontendConfig is the browser-safe subset of deployment settings.
type FrontendConfig struct {
	TraceSearch    *TraceSearchSettings    `mapstructure:"trace_search"`
	LangfuseSearch *LangfuseSearchSettings `mapstructure:"langfuse_search"`
}

type TraceSearchSettings struct {
	GrafanaBaseURL        string `mapstructure:"grafana_base_url"`
	TempoDatasourceUID    string `mapstructure:"tempo_datasource_uid"`
	DeploymentEnvironment string `mapstructure:"deployment_environment"`
}

type LangfuseSearchSettings struct {
	BaseURL                          string `mapstructure:"base_url"`
	ProjectID                        string `mapstructure:"project_id"`
	SessionMetadataKey               string `mapstructure:"session_metadata_key"`
	SandboxNamespaceMetadataKey      string `mapstructure:"sandbox_namespace_metadata_key"`
	SandboxServiceAccountMetadataKey string `mapstructure:"sandbox_service_account_metadata_key"`
}

func (c FrontendConfig) validate() error {
	required := map[string]string{}
	if trace := c.TraceSearch; trace != nil {
		if err := validateHTTPSURL("frontend_config.trace_search.grafana_base_url", trace.GrafanaBaseURL); err != nil {
			return err
		}
		required["frontend_config.trace_search.tempo_datasource_uid"] = trace.TempoDatasourceUID
		required["frontend_config.trace_search.deployment_environment"] = trace.DeploymentEnvironment
	}
	if langfuse := c.LangfuseSearch; langfuse != nil {
		if err := validateHTTPSURL("frontend_config.langfuse_search.base_url", langfuse.BaseURL); err != nil {
			return err
		}
		required["frontend_config.langfuse_search.project_id"] = langfuse.ProjectID
		required["frontend_config.langfuse_search.session_metadata_key"] = langfuse.SessionMetadataKey
		required["frontend_config.langfuse_search.sandbox_namespace_metadata_key"] = langfuse.SandboxNamespaceMetadataKey
		required["frontend_config.langfuse_search.sandbox_service_account_metadata_key"] = langfuse.SandboxServiceAccountMetadataKey
	}
	for name, value := range required {
		if value == "" {
			return fmt.Errorf("%s must not be empty", name)
		}
	}
	return nil
}

func validateHTTPSURL(name, raw string) error {
	if err := validateHTTPURL(name, raw); err != nil {
		return err
	}
	parsed, _ := url.Parse(raw)
	if parsed.Scheme != "https" {
		return fmt.Errorf("%s must use HTTPS", name)
	}
	return nil
}
