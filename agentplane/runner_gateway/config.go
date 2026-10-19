package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
)

type accountFlags []auth.Account

func (a *accountFlags) String() string {
	parts := make([]string, 0, len(*a))
	for _, account := range *a {
		parts = append(parts, account.Namespace+"/"+account.Name)
	}
	return strings.Join(parts, ",")
}

func parseAccount(value string) (auth.Account, error) {
	parts := strings.Split(value, "/")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return auth.Account{}, fmt.Errorf("service account must be namespace/name: %q", value)
	}
	return auth.Account{Namespace: parts[0], Name: parts[1]}, nil
}

func (a *accountFlags) Set(value string) error {
	account, err := parseAccount(value)
	if err != nil {
		return err
	}
	*a = append(*a, account)
	return nil
}

type config struct {
	Namespace                                           string
	DispatcherDatabaseURL                               string
	PodName, PodIP                                      string
	RunnerTokenFile                                     string
	Kubeconfig                                          string
	ListenAddress, HTTPListenAddress                    string
	ProxyPort                                           int
	RunnerControlTargetTemplate                         string
	DispatcherAudience, GatewayAudience, RunnerAudience string
	DispatcherCallers, GatewayCallers, WakeCallers      []auth.Account
	ControllerAccount                                   *auth.Account
	QueueCap                                            int
	WatchLease, RunnerTimeout                           time.Duration
}

// loadConfig keeps serving configuration explicit at the deployment boundary.
// Only the database URL and Pod identity are environment values: the former is
// a Secret and the Pod IP is supplied by the Downward API. Kubernetes sets the
// Pod hostname to its name unless an explicit hostname is configured.
func loadConfig(args []string, getenv func(string) string) (config, error) {
	var c config
	flags := flag.NewFlagSet("runner-gateway", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	flags.StringVar(&c.Namespace, "sandbox-namespace", "", "namespace of Agentplane Sandboxes")
	flags.StringVar(&c.RunnerTokenFile, "runner-token-file", "", "projected Gateway audience token for cross-replica Runner calls")
	flags.StringVar(&c.Kubeconfig, "kubeconfig", "", "Kubernetes client config; defaults to in-cluster credentials")
	flags.StringVar(&c.ListenAddress, "listen-address", ":8080", "gRPC listen address for Dispatcher, Runner proxy and runner Connect")
	flags.StringVar(&c.HTTPListenAddress, "http-listen-address", "", "HTTP/JSON listen address for the Dispatcher's generated facade; empty serves none")
	flags.IntVar(&c.ProxyPort, "proxy-port", sandboxapi.ControlServicePort, "port advertised in each Sandbox control EndpointSlice")
	flags.StringVar(&c.RunnerControlTargetTemplate, "runner-control-target-template", sandboxapi.ControlServiceTargetTemplate(), "target or target template the Dispatcher uses for each Sandbox runner proxy")
	flags.StringVar(&c.DispatcherAudience, "dispatcher-audience", "", "Dispatcher caller TokenReview audience")
	flags.StringVar(&c.GatewayAudience, "gateway-audience", "", "Gateway caller TokenReview audience")
	flags.StringVar(&c.RunnerAudience, "runner-audience", "", "runner Connect TokenReview audience")
	flags.IntVar(&c.QueueCap, "max-queued-per-session", 1000, "queued commands per Session")
	flags.DurationVar(&c.WatchLease, "dispatcher-watch-lease", 300*time.Second, "WatchDispatches stream lease")
	flags.DurationVar(&c.RunnerTimeout, "runner-request-timeout", 10*time.Second, "per-Sandbox Runner call timeout")
	flags.Var((*accountFlags)(&c.DispatcherCallers), "dispatcher-caller", "allowed Dispatcher ServiceAccount namespace/name (repeatable)")
	flags.Var((*accountFlags)(&c.GatewayCallers), "gateway-caller", "allowed Gateway Runner proxy ServiceAccount namespace/name (repeatable)")
	flags.Var((*accountFlags)(&c.WakeCallers), "wake-caller", "Dispatcher caller allowed to wake on send (repeatable)")
	controller := flags.String("sandbox-controller-account", "", "ServiceAccount allowed to Quiesce through Gateway")
	if err := flags.Parse(args); err != nil {
		return config{}, err
	}
	if flags.NArg() != 0 {
		return config{}, errors.New("unexpected positional arguments")
	}
	if *controller != "" {
		account, err := parseAccount(*controller)
		if err != nil {
			return config{}, err
		}
		c.ControllerAccount = &account
	}
	c.DispatcherDatabaseURL = getenv("AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL")
	c.PodName = getenv("AGENTPLANE_RUNNER_GATEWAY_POD_NAME")
	if c.PodName == "" {
		var err error
		c.PodName, err = os.Hostname()
		if err != nil {
			return config{}, err
		}
	}
	c.PodIP = getenv("AGENTPLANE_RUNNER_GATEWAY_POD_IP")
	if c.Namespace == "" || c.DispatcherDatabaseURL == "" || c.PodName == "" || c.PodIP == "" || c.RunnerTokenFile == "" ||
		c.DispatcherAudience == "" || c.GatewayAudience == "" || c.RunnerAudience == "" ||
		len(c.DispatcherCallers) == 0 || len(c.GatewayCallers) == 0 || c.QueueCap < 1 || c.WatchLease <= 0 || c.RunnerTimeout <= 0 ||
		c.ListenAddress == "" || c.ProxyPort < 1 || c.ProxyPort > 65535 ||
		c.RunnerControlTargetTemplate == "" {
		return config{}, errors.New("Runner Gateway identity, policy, database and positive timeout settings are required")
	}
	allowed := make(map[auth.Account]struct{}, len(c.DispatcherCallers))
	for _, account := range c.DispatcherCallers {
		allowed[account] = struct{}{}
	}
	for _, account := range c.WakeCallers {
		if _, okay := allowed[account]; !okay {
			return config{}, fmt.Errorf("wake caller %s/%s is not a Dispatcher caller", account.Namespace, account.Name)
		}
	}
	return c, nil
}
