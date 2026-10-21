package main

import (
	"errors"
	"fmt"
	"math"
	"os"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/actions"
	"github.com/agentydragon/ducktape/agentplane/notification_service/github"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/spf13/viper"
)

const settingPrefix = "AGENTPLANE_NOTIFICATIONS_"

// tokenAudience is what the TokenReview of a caller's bearer requires unless `token_audience` names another.
const tokenAudience = "agentplane-notifications"

// config is the Notification Service's deployment contract: each setting is a YAML key of the file named by
// AGENTPLANE_NOTIFICATIONS_CONFIG_FILE, and each key, a nested one with its dots written as `__`, also names an
// AGENTPLANE_NOTIFICATIONS_* environment variable that overrides it (`github.private_key` is
// AGENTPLANE_NOTIFICATIONS_GITHUB__PRIVATE_KEY). Unknown keys in the file are refused. The package settings
// (worker, actions, github) are the structs those packages own, at the keys their mapstructure tags name.
//
// The database URL, the GitHub private key and the webhook secret are Secrets, so a deployment supplies them as
// environment variables.
type config struct {
	DatabaseURL string `mapstructure:"database_url"`
	// Namespace holds the Sandboxes that destinations may name and the callers the service admits.
	Namespace     string `mapstructure:"namespace"`
	TokenAudience string `mapstructure:"token_audience"`
	// OperatorReaderAccount is the name, in Namespace, of the ServiceAccount that may call NotificationsOperator.
	// Empty admits nobody.
	OperatorReaderAccount string       `mapstructure:"operator_reader_account"`
	Quotas                store.Quotas `mapstructure:"quotas"`
	worker.Settings       `mapstructure:",squash"`
	Actions               actions.Settings `mapstructure:"actions"`
	RunnerGateway         runnerGateway    `mapstructure:"runner_gateway"`
	HistoryService        peer             `mapstructure:"history_service"`
	SessionDispatcher     peer             `mapstructure:"session_dispatcher"`
	// GitHub is on when any of its settings is set.
	GitHub github.Settings `mapstructure:"github"`
	// RequestTimeoutSeconds bounds a call's backend work and each write to a watch stream.
	RequestTimeoutSeconds float64 `mapstructure:"request_timeout_s"`
	// WatchLeaseSeconds is how long a watch stream lasts before planned renewal.
	WatchLeaseSeconds float64 `mapstructure:"watch_lease_s"`
	// MessageBodyLimitBytes is the most UTF-8 bytes a message body may hold, at most store.MessageBodyCeiling.
	MessageBodyLimitBytes int `mapstructure:"message_body_limit_bytes"`
	// PolicyWatchPeriodSeconds is how long the API server holds each watch of PolicySets and PolicyBindings open.
	// Sends are refused once a watch has missed reader.StaleAfterCycles of these.
	PolicyWatchPeriodSeconds int    `mapstructure:"policy_watch_period_s"`
	Host                     string `mapstructure:"host"`
	Port                     int    `mapstructure:"port"`
}

// runnerGateway is how the service reaches a Sandbox's runner through the Runner Gateway's proxy.
type runnerGateway struct {
	// ControlTarget is each Sandbox's control Service, a template of `{sandbox}` and `{namespace}` such as
	// `{sandbox}-control.{namespace}.svc:8080`.
	ControlTarget         string  `mapstructure:"control_target"`
	TokenFile             string  `mapstructure:"token_file"`
	RequestTimeoutSeconds float64 `mapstructure:"request_timeout_s"`
}

// peer is another Agentplane service the service calls over gRPC as a workload.
type peer struct {
	// Target is the gRPC `host:port`.
	Target string `mapstructure:"target"`
	// TokenFile is the projected ServiceAccount token the peer admits, read again for each call.
	TokenFile             string  `mapstructure:"token_file"`
	RequestTimeoutSeconds float64 `mapstructure:"request_timeout_s"`
}

func defaultConfig() config {
	return config{
		TokenAudience: tokenAudience,
		Quotas: store.Quotas{
			InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000, UnacknowledgedMessagesPerSender: 128,
		},
		Settings:      worker.DefaultSettings(),
		Actions:       actions.DefaultSettings(),
		RunnerGateway: runnerGateway{RequestTimeoutSeconds: 5},
		// A delivery reads a page of a Session's entries, which can be large.
		HistoryService:        peer{RequestTimeoutSeconds: 20},
		SessionDispatcher:     peer{RequestTimeoutSeconds: 15},
		GitHub:                github.DefaultSettings(),
		RequestTimeoutSeconds: 15, WatchLeaseSeconds: 300,
		MessageBodyLimitBytes: 32 * 1024,
		// A wedged watch then refuses sends within three minutes; a healthy one costs a re-watch a minute.
		PolicyWatchPeriodSeconds: 60,
		Host:                     "0.0.0.0",
		Port:                     8080,
	}
}

func loadConfig() (config, error) {
	c := defaultConfig()
	// Without ExperimentalBindStruct, Unmarshal reads an environment variable only for a bound key.
	settings := viper.NewWithOptions(viper.ExperimentalBindStruct(), viper.EnvKeyReplacer(strings.NewReplacer(".", "__")))
	settings.SetEnvPrefix("AGENTPLANE_NOTIFICATIONS")
	settings.AutomaticEnv()
	if path := os.Getenv(settingPrefix + "CONFIG_FILE"); path != "" {
		settings.SetConfigFile(path)
		settings.SetConfigType("yaml")
		if err := settings.ReadInConfig(); err != nil {
			return config{}, errors.New("could not read Notification Service settings file")
		}
	}
	// The errors name no value: a setting can be a Secret.
	if err := settings.UnmarshalExact(&c); err != nil {
		return config{}, errors.New("invalid Notification Service settings")
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

// githubEnabled is whether any GitHub setting differs from its default. The settings without a default are the
// App's ID and secrets, so a block that sets nothing else is not one.
func (c *config) githubEnabled() bool { return c.GitHub != github.DefaultSettings() }

func (c *config) validate() error {
	for _, required := range []struct{ name, value string }{
		{"database_url", c.DatabaseURL},
		{"namespace", c.Namespace},
		{"token_audience", c.TokenAudience},
		{"host", c.Host},
		{"runner_gateway.control_target", c.RunnerGateway.ControlTarget},
		{"runner_gateway.token_file", c.RunnerGateway.TokenFile},
		{"history_service.target", c.HistoryService.Target},
		{"history_service.token_file", c.HistoryService.TokenFile},
		{"session_dispatcher.target", c.SessionDispatcher.Target},
		{"session_dispatcher.token_file", c.SessionDispatcher.TokenFile},
	} {
		if required.value == "" {
			return fmt.Errorf("%s is required", required.name)
		}
	}
	if c.Port < 1 || c.Port > 65535 {
		return errors.New("port must be between 1 and 65535")
	}
	for _, quota := range []struct {
		name  string
		value int
	}{
		{"quotas.inboxes_per_account", c.Quotas.InboxesPerAccount},
		{"quotas.active_subscriptions_per_inbox", c.Quotas.ActiveSubscriptionsPerInbox},
		{"quotas.entries_per_inbox", c.Quotas.EntriesPerInbox},
		{"quotas.unacknowledged_messages_per_sender", c.Quotas.UnacknowledgedMessagesPerSender},
	} {
		if quota.value < 1 {
			return fmt.Errorf("%s must be at least 1", quota.name)
		}
	}
	for _, timeout := range []struct {
		name  string
		value float64
	}{
		{"runner_gateway.request_timeout_s", c.RunnerGateway.RequestTimeoutSeconds},
		{"history_service.request_timeout_s", c.HistoryService.RequestTimeoutSeconds},
		{"session_dispatcher.request_timeout_s", c.SessionDispatcher.RequestTimeoutSeconds},
		{"request_timeout_s", c.RequestTimeoutSeconds},
		{"watch_lease_s", c.WatchLeaseSeconds},
	} {
		if !(timeout.value > 0) || math.IsInf(timeout.value, 0) {
			return fmt.Errorf("%s must be a finite number above 0", timeout.name)
		}
	}
	if c.MessageBodyLimitBytes < 1 || c.MessageBodyLimitBytes > store.MessageBodyCeiling {
		return fmt.Errorf("message_body_limit_bytes must be 1 to %d", store.MessageBodyCeiling)
	}
	if time.Duration(c.PolicyWatchPeriodSeconds)*time.Second < reader.MinWatchPeriod {
		return fmt.Errorf("policy_watch_period_s must be at least %v", reader.MinWatchPeriod.Seconds())
	}
	if err := c.Settings.Validate(); err != nil {
		return err
	}
	if err := c.Actions.Validate(); err != nil {
		return err
	}
	if c.githubEnabled() {
		return c.GitHub.Validate()
	}
	return nil
}

// seconds saturates a huge setting instead of overflowing time.Duration.
func seconds(value float64) time.Duration {
	return time.Duration(min(value, math.MaxInt32) * float64(time.Second))
}
