package main

import (
	"errors"
	"fmt"
	"math"
	"net"
	"net/url"
	"os"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/configfile"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	inboxstore "github.com/agentydragon/ducktape/agentplane/notification_service/internal/inbox"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/settings"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/worker"
)

const settingPrefix = "AGENTPLANE_NOTIFICATIONS_"

// config is the Notification Service's deployment contract. Static settings come only from the YAML file named by
// AGENTPLANE_NOTIFICATIONS_CONFIG_FILE. The database URL, GitHub private key and webhook secret are narrow runtime
// Secret inputs read explicitly from their existing environment keys after static settings are decoded.
type config struct {
	DatabaseURL string `mapstructure:"-"`
	// Namespace holds the Sandboxes that destinations may name and the callers the service admits.
	Namespace string `mapstructure:"namespace"`
	// OperatorReaderServices are exact service SVIDs admitted to the same read-only diagnostic methods.
	OperatorReaderServices []string          `mapstructure:"operator_reader_services"`
	Quotas                 inboxstore.Quotas `mapstructure:"quotas"`
	worker.Settings        `mapstructure:",squash"`
	GitHubRetention        settings.Retention `mapstructure:",squash"`
	Actions                actionPeer         `mapstructure:"actions"`
	RunnerGateway          runnerGateway      `mapstructure:"runner_gateway"`
	HistoryService         peer               `mapstructure:"history_service"`
	SessionDispatcher      peer               `mapstructure:"session_dispatcher"`
	// GitHub is on when any of its settings is set.
	GitHub settings.Settings `mapstructure:"github"`
	// RequestTimeoutSeconds bounds a call's backend work and each write to a watch stream.
	RequestTimeoutSeconds float64 `mapstructure:"request_timeout_s"`
	// MessageBodyLimitBytes is the most UTF-8 bytes a message body may hold, at most inboxstore.MessageBodyCeiling.
	MessageBodyLimitBytes int    `mapstructure:"message_body_limit_bytes"`
	Host                  string `mapstructure:"host"`
	// HTTPPort is reserved for the signed GitHub webhook.
	HTTPPort int             `mapstructure:"http_port"`
	RPC      listener.Config `mapstructure:"rpc"`
}

// runnerGateway is how the service reaches a Sandbox's Runtime through the Environments proxy.
type runnerGateway struct {
	TLS *peertls.ClientTLS `mapstructure:"tls"`
	// ControlTarget is the Environments Runtime proxy Service, a template of `{namespace}` such as
	// `environments.{namespace}.svc:8082`.
	ControlTarget string `mapstructure:"control_target"`
	// TokenFile is the projected token used when TLS does not present a client SVID.
	TokenFile             string  `mapstructure:"token_file"`
	RequestTimeoutSeconds float64 `mapstructure:"request_timeout_s"`
}

// peer is another Agentplane service the service calls over gRPC as a workload.
type peer struct {
	TLS *peertls.ClientTLS `mapstructure:"tls"`
	// Target is the gRPC `host:port`.
	Target string `mapstructure:"target"`
	// TokenFile is the projected token used when TLS does not present a client SVID.
	TokenFile             string  `mapstructure:"token_file"`
	RequestTimeoutSeconds float64 `mapstructure:"request_timeout_s"`
}

// actionPeer retains the deployment's HTTP(S) URL key while composing the Action Service's gRPC transport.
type actionPeer struct {
	TLS *peertls.ClientTLS `mapstructure:"tls"`
	URL string             `mapstructure:"url"`
	// TokenFile is the projected token used when TLS does not present a client SVID.
	TokenFile             string  `mapstructure:"token_file"`
	RequestTimeoutSeconds float64 `mapstructure:"request_timeout_s"`
}

type actionGRPCEndpoint struct {
	target        string
	tlsServerName string
}

func (p actionPeer) grpcEndpoint() (actionGRPCEndpoint, error) {
	parsed, err := url.Parse(p.URL)
	if err != nil {
		return actionGRPCEndpoint{}, errors.New("actions.url must be an http or https origin without credentials, path, query or fragment")
	}
	scheme := strings.ToLower(parsed.Scheme)
	if (scheme != "http" && scheme != "https") || parsed.Hostname() == "" ||
		parsed.User != nil || parsed.Opaque != "" || (parsed.Path != "" && parsed.Path != "/") || parsed.RawPath != "" ||
		parsed.RawQuery != "" || parsed.ForceQuery || parsed.Fragment != "" || parsed.RawFragment != "" ||
		strings.ContainsAny(p.URL, "?#") || strings.HasSuffix(parsed.Host, ":") {
		return actionGRPCEndpoint{}, errors.New("actions.url must be an http or https origin without credentials, path, query or fragment")
	}
	port := parsed.Port()
	if port == "" {
		if scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	} else {
		number, err := strconv.Atoi(port)
		if err != nil || number < 1 || number > 65535 {
			return actionGRPCEndpoint{}, errors.New("actions.url has an invalid port")
		}
		port = strconv.Itoa(number)
	}
	endpoint := actionGRPCEndpoint{target: net.JoinHostPort(parsed.Hostname(), port)}
	if scheme == "https" {
		endpoint.tlsServerName = parsed.Hostname()
	}
	return endpoint, nil
}

func defaultConfig() config {
	return config{
		Quotas: inboxstore.Quotas{
			InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000, UnacknowledgedMessagesPerSender: 128,
		},
		Settings:        worker.DefaultSettings(),
		GitHubRetention: settings.DefaultRetention(),
		Actions:         actionPeer{RequestTimeoutSeconds: 5},
		RunnerGateway:   runnerGateway{RequestTimeoutSeconds: 5},
		// A delivery reads a page of a Session's entries, which can be large.
		HistoryService:        peer{RequestTimeoutSeconds: 20},
		SessionDispatcher:     peer{RequestTimeoutSeconds: 15},
		GitHub:                settings.DefaultSettings(),
		RequestTimeoutSeconds: 15,
		MessageBodyLimitBytes: 32 * 1024,
		Host:                  "0.0.0.0",
		RPC:                   listener.Config{ListenAddress: "0.0.0.0:8080"},
		HTTPPort:              8081,
	}
}

func loadConfig() (config, error) {
	path := strings.TrimSpace(os.Getenv(settingPrefix + "CONFIG_FILE"))
	if path == "" {
		return config{}, errors.New("Notification Service settings file path is required")
	}
	c := defaultConfig()
	raw, err := configfile.DecodeMapstructureFile(path, &c, configfile.MapstructureOptions{
		DecodeHook: listener.AuthenticationDecodeHook,
	})
	if err != nil {
		return config{}, configfile.RedactMapstructureError("Notification Service", err)
	}
	if raw == nil {
		return config{}, errors.New("Notification Service settings file must contain a YAML mapping")
	}

	// Preserve absent (default seven days) versus explicit YAML null (retain indefinitely).
	if value, exists := raw["github_delivery_retention_days"]; exists && value == nil {
		c.GitHubRetention.Days = nil
	}
	// Only these runtime credentials are read from environment variables; static settings never consult the process environment.
	c.DatabaseURL = os.Getenv(settingPrefix + "DATABASE_URL")
	c.GitHub.PrivateKey = os.Getenv(settingPrefix + "GITHUB__PRIVATE_KEY")
	c.GitHub.WebhookSecret = os.Getenv(settingPrefix + "GITHUB__WEBHOOK_SECRET")
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

// githubEnabled is whether any GitHub setting differs from its default. The settings without a default are the
// App's ID and secrets, so a block that sets nothing else is not one.
func (c *config) githubEnabled() bool { return c.GitHub != settings.DefaultSettings() }

func (c *config) validate() error {
	for _, required := range []struct{ name, value string }{
		{"database_url", c.DatabaseURL},
		{"namespace", c.Namespace},
		{"host", c.Host},
		{"actions.url", c.Actions.URL},
		{"runner_gateway.control_target", c.RunnerGateway.ControlTarget},
		{"history_service.target", c.HistoryService.Target},
		{"session_dispatcher.target", c.SessionDispatcher.Target},
	} {
		if required.value == "" {
			return fmt.Errorf("%s is required", required.name)
		}
	}
	if _, err := c.Actions.grpcEndpoint(); err != nil {
		return err
	}
	for _, caller := range []struct {
		name               string
		tls                *peertls.ClientTLS
		tokenFile          string
		expectedServerURIs []string
	}{
		{name: "actions", tls: c.Actions.TLS, tokenFile: c.Actions.TokenFile, expectedServerURIs: []string{peertls.ActionsURI}},
		{name: "runner_gateway", tls: c.RunnerGateway.TLS, tokenFile: c.RunnerGateway.TokenFile, expectedServerURIs: []string{peertls.SessionDispatcherURI, peertls.SandboxControllerURI}},
		{name: "history_service", tls: c.HistoryService.TLS, tokenFile: c.HistoryService.TokenFile, expectedServerURIs: []string{peertls.HistoryIngressURI}},
		{name: "session_dispatcher", tls: c.SessionDispatcher.TLS, tokenFile: c.SessionDispatcher.TokenFile, expectedServerURIs: []string{peertls.SessionDispatcherURI}},
	} {
		if err := caller.tls.ValidateCaller(caller.tokenFile, peertls.NotificationsURI); err != nil {
			return fmt.Errorf("%s caller authentication: %w", caller.name, err)
		}
		if caller.tls != nil && !slices.Contains(caller.expectedServerURIs, caller.tls.ServerURI) {
			return fmt.Errorf("%s tls.server_uri must be one of %q", caller.name, caller.expectedServerURIs)
		}
	}
	if c.HTTPPort < 1 || c.HTTPPort > 65535 {
		return errors.New("http_port must be between 1 and 65535")
	}
	if err := c.RPC.Validate(); err != nil {
		return fmt.Errorf("rpc listener: %w", err)
	}
	if err := auth.ValidateServiceCallers(c.OperatorReaderServices); err != nil {
		return fmt.Errorf("invalid operator_reader_services: %w", err)
	}
	if !c.RPC.Has(listener.SPIFFE) {
		return errors.New("rpc requires spiffe client trust")
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
		{"actions.request_timeout_s", c.Actions.RequestTimeoutSeconds},
		{"history_service.request_timeout_s", c.HistoryService.RequestTimeoutSeconds},
		{"session_dispatcher.request_timeout_s", c.SessionDispatcher.RequestTimeoutSeconds},
		{"request_timeout_s", c.RequestTimeoutSeconds},
	} {
		if !(timeout.value > 0) || math.IsInf(timeout.value, 0) {
			return fmt.Errorf("%s must be a finite number above 0", timeout.name)
		}
	}
	if c.MessageBodyLimitBytes < 1 || c.MessageBodyLimitBytes > inboxstore.MessageBodyCeiling {
		return fmt.Errorf("message_body_limit_bytes must be 1 to %d", inboxstore.MessageBodyCeiling)
	}
	if err := c.Settings.Validate(); err != nil {
		return err
	}
	if err := c.GitHubRetention.Validate(); err != nil {
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
