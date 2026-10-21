package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/api"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/go-viper/mapstructure/v2"
	"github.com/spf13/viper"
	"gopkg.in/yaml.v3"
)

const settingPrefix = "AGENTPLANE_ACTIONS_"

// config is the Go Action Service's deployment contract: each setting is a YAML key of the file named by
// AGENTPLANE_ACTIONS_CONFIG_FILE, and each key but `action_groups`, a nested one with its dots written as `__`, also
// names an AGENTPLANE_ACTIONS_* environment variable that overrides it (`operator_oidc.issuer` is
// AGENTPLANE_ACTIONS_OPERATOR_OIDC__ISSUER). Unknown keys in the file are refused. A key has the name and meaning it
// has in the Python service's settings (settings.py), which serve the same deployment.
//
// The database URL is a Secret, so a deployment supplies it as an environment variable.
type config struct {
	DatabaseURL string `mapstructure:"database_url"`
	Host        string `mapstructure:"host"`
	Port        int    `mapstructure:"port"`
	// TokenAudience is the audience a workload's projected token must be issued for.
	TokenAudience string `mapstructure:"token_audience"`
	// CallerServiceAccountNamespaces hold the ServiceAccounts whose workloads may call; a labeled account outside
	// them is refused.
	CallerServiceAccountNamespaces []string `mapstructure:"caller_service_account_namespaces"`
	// ReaderAccounts read every account's requests and change none, written `namespace/name` or as a mapping.
	ReaderAccounts []auth.Account `mapstructure:"reader_accounts"`
	// OperatorOIDC is on when any of its settings differs from its default; off, no operator is admitted.
	OperatorOIDC api.OperatorOIDCSettings `mapstructure:"operator_oidc"`
	// ActionGroups is read from the file alone; see settingsFile.
	ActionGroups *catalog.Catalog `mapstructure:"-"`
}

func defaultConfig() config {
	return config{Host: "0.0.0.0", Port: 8080, OperatorOIDC: api.DefaultOperatorOIDCSettings(), ActionGroups: &catalog.Catalog{}}
}

func loadConfig() (config, error) {
	c := defaultConfig()
	// Without ExperimentalBindStruct, Unmarshal reads an environment variable only for a bound key.
	settings := viper.NewWithOptions(viper.ExperimentalBindStruct(), viper.EnvKeyReplacer(strings.NewReplacer(".", "__")))
	settings.SetEnvPrefix("AGENTPLANE_ACTIONS")
	settings.AutomaticEnv()
	if path := os.Getenv(settingPrefix + "CONFIG_FILE"); path != "" {
		file, err := readSettingsFile(path)
		if err != nil {
			return config{}, err
		}
		if c.ActionGroups, err = catalog.New(file.ActionGroups); err != nil {
			return config{}, fmt.Errorf("action_groups: %w", err)
		}
		if err := settings.MergeConfigMap(file.Others); err != nil {
			return config{}, errors.New("could not read Action Service settings file")
		}
	}
	for _, entry := range os.Environ() {
		name, value, _ := strings.Cut(entry, "=")
		if value != "" && (name == settingPrefix+"ACTION_GROUPS" || strings.HasPrefix(name, settingPrefix+"ACTION_GROUPS__")) {
			return config{}, errors.New("action_groups is read from the settings file only")
		}
	}
	// The errors name no value: a setting can be a Secret.
	if err := settings.UnmarshalExact(&c, viper.DecodeHook(mapstructure.TextUnmarshallerHookFunc())); err != nil {
		return config{}, errors.New("invalid Action Service settings")
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

// operatorsEnabled is whether any operator OIDC setting differs from its default.
func (c *config) operatorsEnabled() bool { return c.OperatorOIDC != api.DefaultOperatorOIDCSettings() }

func (c *config) validate() error {
	for _, required := range []struct{ name, value string }{
		{"database_url", c.DatabaseURL},
		{"host", c.Host},
		{"token_audience", c.TokenAudience},
	} {
		if required.value == "" {
			return errors.New(required.name + " is required")
		}
	}
	if c.Port < 1 || c.Port > 65535 {
		return errors.New("port must be between 1 and 65535")
	}
	if len(c.CallerServiceAccountNamespaces) == 0 {
		return errors.New("caller_service_account_namespaces must list at least one namespace")
	}
	if c.operatorsEnabled() {
		return c.OperatorOIDC.Validate()
	}
	return nil
}

// settingsFile is the settings file as gopkg.in/yaml.v3 decodes it.
type settingsFile struct {
	// ActionGroups is decoded apart from Viper, which folds the case of every key it reads: an input schema's keys and
	// an MCP server configuration's keep theirs.
	ActionGroups catalog.Settings `yaml:"action_groups"`
	// Others are the settings Viper reads.
	Others map[string]any `yaml:",inline"`
}

func readSettingsFile(path string) (settingsFile, error) {
	var file settingsFile
	document, err := os.ReadFile(path)
	if err != nil {
		return file, errors.New("could not read Action Service settings file")
	}
	decoder := yaml.NewDecoder(bytes.NewReader(document))
	decoder.KnownFields(true)
	// An empty file is io.EOF: no settings.
	if err := decoder.Decode(&file); err != nil && !errors.Is(err, io.EOF) {
		return file, fmt.Errorf("settings file: %w", withoutValues(err))
	}
	return file, nil
}

// quotedValue is the value a yaml.v3 type error quotes, from its opening backtick to the error's last.
var quotedValue = regexp.MustCompile("(?s) `.*`")

// withoutValues is err with the values yaml.v3 quotes cut out: a setting can be a credential.
func withoutValues(err error) error {
	var typeError *yaml.TypeError
	if !errors.As(err, &typeError) {
		return err
	}
	problems := make([]string, len(typeError.Errors))
	for i, problem := range typeError.Errors {
		problems[i] = quotedValue.ReplaceAllString(problem, "")
	}
	return errors.New(strings.Join(problems, "; "))
}
