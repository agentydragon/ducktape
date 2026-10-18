package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"maps"
	"net/netip"
	"os"
	"reflect"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/caarlos0/env/v11"
	"gopkg.in/yaml.v3"
)

const (
	settingPrefix = "AGENTPLANE_EGRESS_"
	configFileEnv = settingPrefix + "CONFIG_FILE"
)

// config is the proxy's deployment contract. Each setting is a YAML key of the file named by
// AGENTPLANE_EGRESS_CONFIG_FILE, an AGENTPLANE_EGRESS_* environment variable and a --kebab-case flag,
// and a flag overrides the environment, which overrides the file. A list or map given as an
// environment variable or a flag is JSON.
type config struct {
	RulesNamespace                  string            `yaml:"rules_namespace"`
	CredentialsNamespace            string            `yaml:"credentials_namespace"`
	AllowedServiceAccountNamespaces []string          `yaml:"allowed_service_account_namespaces"`
	ListenHost                      string            `yaml:"listen_host"`
	ListenPort                      int               `yaml:"listen_port"`
	AdminHost                       string            `yaml:"admin_host"`
	AdminPort                       int               `yaml:"admin_port"`
	AgentAPIHost                    string            `yaml:"agent_api_host"`
	AgentAPIPort                    int               `yaml:"agent_api_port"`
	CACert                          string            `yaml:"ca_cert"`
	CAKey                           string            `yaml:"ca_key"`
	UpstreamCAFile                  string            `yaml:"upstream_ca_file"`
	TokenAudience                   string            `yaml:"token_audience"`
	ProjectedTokenAudiences         []string          `yaml:"projected_token_audiences"`
	Kubeconfig                      string            `yaml:"kubeconfig"`
	LogLevels                       map[string]string `yaml:"log_levels"`
	ResyncSeconds                   int               `yaml:"resync_seconds"`
	DatabaseURL                     string            `yaml:"database_url"`
	DecisionHistorySize             int               `yaml:"decision_history_size"`
	DecisionRetentionDays           int               `yaml:"decision_retention_days"`
	DecisionQueueSize               int               `yaml:"decision_queue_size"`
	DecisionBatchSize               int               `yaml:"decision_batch_size"`
	ExemptNetworks                  []netip.Prefix    `yaml:"exempt_networks"`
}

func defaultConfig() config {
	return config{
		ListenHost: "0.0.0.0", ListenPort: 8888, AdminHost: "0.0.0.0", AdminPort: 8081,
		AgentAPIHost: "0.0.0.0", AgentAPIPort: 8082, ResyncSeconds: 300, DecisionHistorySize: 200,
		DecisionRetentionDays: 7, DecisionQueueSize: 2000, DecisionBatchSize: 100,
	}
}

// environmentOptions makes env read each field from AGENTPLANE_EGRESS_<FIELD NAME IN UPPER SNAKE
// CASE>, the field's YAML key upper-cased, and a list or map as JSON.
func environmentOptions(environment map[string]string) env.Options {
	return env.Options{
		Environment: environment, Prefix: settingPrefix, UseFieldNameByDefault: true,
		FuncMap: map[reflect.Type]env.ParserFunc{
			reflect.TypeFor[[]string]():          parseJSON[[]string],
			reflect.TypeFor[map[string]string](): parseJSON[map[string]string],
			reflect.TypeFor[[]netip.Prefix]():    parseJSON[[]netip.Prefix],
		},
	}
}

func parseJSON[T any](text string) (any, error) {
	var value T
	decoder := json.NewDecoder(strings.NewReader(text))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&value) != nil || !errors.Is(decoder.Decode(new(any)), io.EOF) {
		return nil, errors.New("not a JSON value of its type")
	}
	return value, nil
}

// loadConfig applies defaults, then the settings file, then environ, then args.
func loadConfig(args, environ []string) (config, error) {
	c := defaultConfig()
	environment := make(map[string]string, len(environ))
	for _, item := range environ {
		if name, value, found := strings.Cut(item, "="); found {
			environment[name] = value
		}
	}
	if path := environment[configFileEnv]; path != "" {
		if err := readSettingsFile(path, &c); err != nil {
			return config{}, err
		}
	}
	options := environmentOptions(environment)
	flagged, err := flagValues(options, args)
	if err != nil {
		return config{}, err
	}
	maps.Copy(environment, flagged)
	if err := env.ParseWithOptions(&c, options); err != nil {
		return config{}, settingsError(err)
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

// settingsError names the settings that failed to parse and never carries their text: the database
// URL holds a password.
func settingsError(err error) error {
	var aggregate env.AggregateError
	if !errors.As(err, &aggregate) {
		return err
	}
	var failed []string
	for _, failure := range aggregate.Errors {
		var parseFailure env.ParseError
		if !errors.As(failure, &parseFailure) {
			return failure
		}
		field, _ := reflect.TypeFor[config]().FieldByName(parseFailure.Name)
		failed = append(failed, field.Tag.Get("yaml"))
	}
	return fmt.Errorf("settings must be a value of their type, a list or map as JSON: %s", strings.Join(failed, ", "))
}

// readSettingsFile decodes a file that must exist and hold exactly one document of known keys.
func readSettingsFile(path string, c *config) error {
	contents, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("%s=%s is not a readable file", configFileEnv, path)
	}
	decoder := yaml.NewDecoder(bytes.NewReader(contents))
	decoder.KnownFields(true)
	if err := decoder.Decode(c); err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("invalid settings file %s: %w", path, err)
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return fmt.Errorf("settings file %s must hold one document", path)
	}
	return nil
}

// flagValues reads each setting's --kebab-case flag and returns the ones given, keyed by the
// environment variable that gives the same setting.
func flagValues(options env.Options, args []string) (map[string]string, error) {
	fields, err := env.GetFieldParamsWithOptions(&config{}, options)
	if err != nil {
		return nil, err
	}
	flags := flag.NewFlagSet("agentplane-egress", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	given := map[string]string{}
	for _, field := range fields {
		flags.Func(strings.ReplaceAll(strings.ToLower(field.OwnKey), "_", "-"), field.OwnKey, func(value string) error {
			given[field.Key] = value
			return nil
		})
	}
	if err := flags.Parse(args); err != nil {
		return nil, fmt.Errorf("invalid command-line flags: %w", err)
	}
	if flags.NArg() != 0 {
		return nil, errors.New("unexpected positional arguments")
	}
	return given, nil
}

// bounded is an integer setting and the inclusive range it must fall in.
type bounded struct {
	name          string
	value, lo, hi int
}

func (c *config) validate() error {
	for _, required := range []struct{ name, value string }{
		{"rules_namespace", c.RulesNamespace},
		{"credentials_namespace", c.CredentialsNamespace},
		{"ca_cert", c.CACert},
		{"ca_key", c.CAKey},
		{"token_audience", c.TokenAudience},
		{"database_url", c.DatabaseURL},
	} {
		if required.value == "" {
			return fmt.Errorf("%s is required", required.name)
		}
	}
	if len(c.AllowedServiceAccountNamespaces) == 0 || slices.Contains(c.AllowedServiceAccountNamespaces, "") {
		return errors.New("allowed_service_account_namespaces must list at least one namespace, none of them empty")
	}
	if slices.Contains(c.ProjectedTokenAudiences, "") {
		return errors.New("projected_token_audiences must not contain an empty audience")
	}
	for _, limit := range []bounded{
		{"listen_port", c.ListenPort, 0, 65535},
		{"admin_port", c.AdminPort, 0, 65535},
		{"agent_api_port", c.AgentAPIPort, 0, 65535},
		// client-go relists a watch shorter than a second, so the informer takes two at the least.
		{"resync_seconds", c.ResyncSeconds, 2, 86400},
		{"decision_history_size", c.DecisionHistorySize, 1, history.MaxCapacity},
		{"decision_retention_days", c.DecisionRetentionDays, 1, 365},
		{"decision_queue_size", c.DecisionQueueSize, 1, 100000},
		{"decision_batch_size", c.DecisionBatchSize, 1, 1000},
	} {
		if limit.value < limit.lo || limit.value > limit.hi {
			return fmt.Errorf("%s must be between %d and %d", limit.name, limit.lo, limit.hi)
		}
	}
	for logger, level := range c.LogLevels {
		if logger != "root" {
			return errors.New(`log_levels may only name "root"`)
		}
		if _, err := logLevel(level); err != nil {
			return err
		}
	}
	return nil
}
