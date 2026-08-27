package main

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/caarlos0/env/v11"

	kubeapiproxy "github.com/agentydragon/ducktape/haku/kube_api_proxy"
)

type environmentConfig struct {
	AuthorizationURL           url.URL `env:"HAKU_KUBE_AUTHORIZATION_URL,required,notEmpty"`
	AllowInsecureAuthorization bool    `env:"HAKU_KUBE_ALLOW_INSECURE_AUTHORITY" envDefault:"false"`
	ListenAddress              string  `env:"HAKU_KUBE_LISTEN_ADDRESS" envDefault:":8080"`
	// In-cluster TLS listener for kubeconfig callers: kubectl attaches credentials only to an
	// https server, so sandboxes reach this listener while the plaintext one stays the
	// Gateway backend hop. Serving is enabled by supplying both files.
	TLSListenAddress              string        `env:"HAKU_KUBE_TLS_LISTEN_ADDRESS" envDefault:":8443"`
	TLSCertFile                   string        `env:"HAKU_KUBE_TLS_CERT_FILE"`
	TLSKeyFile                    string        `env:"HAKU_KUBE_TLS_KEY_FILE"`
	AuthorizationTimeout          time.Duration `env:"HAKU_KUBE_AUTHORIZATION_TIMEOUT" envDefault:"3s"`
	RequestTimeout                time.Duration `env:"HAKU_KUBE_REQUEST_TIMEOUT" envDefault:"30s"`
	StreamRevalidationInterval    time.Duration `env:"HAKU_KUBE_STREAM_REVALIDATION_INTERVAL" envDefault:"5s"`
	MaxRequestBytes               int64         `env:"HAKU_KUBE_MAX_REQUEST_BYTES" envDefault:"10485760"`
	ServiceAccountDirectory       string        `env:"HAKU_KUBE_SERVICEACCOUNT_DIRECTORY" envDefault:"/var/run/secrets/kubernetes.io/serviceaccount"`
	KubernetesServiceHost         string        `env:"KUBERNETES_SERVICE_HOST,required,notEmpty"`
	KubernetesServicePortHTTPS    string        `env:"KUBERNETES_SERVICE_PORT_HTTPS"`
	KubernetesServicePortFallback string        `env:"KUBERNETES_SERVICE_PORT"`
}

func main() {
	if err := run(); err != nil {
		slog.Error("Haku Kubernetes API proxy stopped", "error", err)
		os.Exit(1)
	}
}

func run() error {
	config, err := loadEnvironmentConfig()
	if err != nil {
		return err
	}
	upstreamURL, transport, err := kubeapiproxy.InClusterUpstream(kubeapiproxy.InClusterConfig{
		ServiceHost:             config.KubernetesServiceHost,
		ServicePort:             config.kubernetesServicePort(),
		ServiceAccountDirectory: config.ServiceAccountDirectory,
	})
	if err != nil {
		return err
	}

	handler, err := kubeapiproxy.NewHandler(kubeapiproxy.Config{
		Upstream:                   upstreamURL,
		UpstreamTransport:          transport,
		AuthorizationURL:           &config.AuthorizationURL,
		AllowInsecureAuthorization: config.AllowInsecureAuthorization,
		AuthorizationTimeout:       config.AuthorizationTimeout,
		RequestTimeout:             config.RequestTimeout,
		StreamRevalidationInterval: config.StreamRevalidationInterval,
		MaxRequestBytes:            config.MaxRequestBytes,
	})
	if err != nil {
		return err
	}

	newServer := func(address string) *http.Server {
		return &http.Server{
			Addr:              address,
			Handler:           handler,
			ReadHeaderTimeout: 5 * time.Second,
			IdleTimeout:       60 * time.Second,
			// Request contexts carry the stricter per-request/lease deadline. Do not
			// add WriteTimeout here: it would make future streaming support subtly
			// depend on a second, unrelated deadline.
		}
	}
	plain := newServer(config.ListenAddress)
	servers := []*http.Server{plain}
	var tlsServer *http.Server
	if config.TLSCertFile != "" {
		tlsServer = newServer(config.TLSListenAddress)
		servers = append(servers, tlsServer)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		for _, server := range servers {
			_ = server.Shutdown(shutdownCtx)
		}
	}()

	// One failing listener stops the process: a proxy that silently lost its TLS endpoint would
	// strand every kubeconfig caller while still answering probes on the plaintext one.
	listenErrors := make(chan error, len(servers))
	go func() { listenErrors <- plain.ListenAndServe() }()
	if tlsServer != nil {
		go func() { listenErrors <- tlsServer.ListenAndServeTLS(config.TLSCertFile, config.TLSKeyFile) }()
	}
	slog.Info(
		"Haku Kubernetes API proxy listening",
		"address", plain.Addr,
		"tls_address", tlsAddress(tlsServer),
		"upstream", upstreamURL.Redacted(),
	)
	if err := <-listenErrors; err != nil && err != http.ErrServerClosed {
		return err
	}
	return nil
}

func tlsAddress(server *http.Server) string {
	if server == nil {
		return "disabled"
	}
	return server.Addr
}

func loadEnvironmentConfig() (environmentConfig, error) {
	return parseEnvironmentConfig(env.Options{})
}

func parseEnvironmentConfig(options env.Options) (environmentConfig, error) {
	config, err := env.ParseAsWithOptions[environmentConfig](options)
	if err != nil {
		return environmentConfig{}, fmt.Errorf("parse environment configuration: %w", err)
	}
	if config.AuthorizationURL.Scheme == "" || config.AuthorizationURL.Host == "" {
		return environmentConfig{}, fmt.Errorf("HAKU_KUBE_AUTHORIZATION_URL must be an absolute URL")
	}
	if config.AuthorizationTimeout <= 0 {
		return environmentConfig{}, fmt.Errorf("HAKU_KUBE_AUTHORIZATION_TIMEOUT must be positive")
	}
	if config.RequestTimeout <= 0 {
		return environmentConfig{}, fmt.Errorf("HAKU_KUBE_REQUEST_TIMEOUT must be positive")
	}
	if config.StreamRevalidationInterval <= 0 {
		return environmentConfig{}, fmt.Errorf("HAKU_KUBE_STREAM_REVALIDATION_INTERVAL must be positive")
	}
	if config.MaxRequestBytes <= 0 {
		return environmentConfig{}, fmt.Errorf("HAKU_KUBE_MAX_REQUEST_BYTES must be positive")
	}
	if (config.TLSCertFile == "") != (config.TLSKeyFile == "") {
		return environmentConfig{}, fmt.Errorf("HAKU_KUBE_TLS_CERT_FILE and HAKU_KUBE_TLS_KEY_FILE must be set together")
	}
	if config.kubernetesServicePort() == "" {
		return environmentConfig{}, fmt.Errorf("KUBERNETES_SERVICE_PORT_HTTPS or KUBERNETES_SERVICE_PORT is required")
	}
	return config, nil
}

func (config environmentConfig) kubernetesServicePort() string {
	if config.KubernetesServicePortHTTPS != "" {
		return config.KubernetesServicePortHTTPS
	}
	return config.KubernetesServicePortFallback
}
