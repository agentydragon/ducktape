// The platform sessions gateway serves HTTP/JSON and optional gRPC management
// while each remote provider owns its session and data plane.
package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/platform_sessions/frontend"
	"github.com/agentydragon/ducktape/platform_sessions/management/gateway"
	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/v1/providerpb"
	"github.com/bazelbuild/rules_go/go/runfiles"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

type instance struct {
	ID      string `json:"id"`
	Type    string `json:"type"`
	Enabled *bool  `json:"enabled"`
}
type instancesConfig struct {
	Integrations []instance `json:"integrations"`
}
type connection struct {
	Origin           string  `json:"origin"`
	ControlTarget    string  `json:"control_target"`
	ControlTokenFile string  `json:"control_token_file"`
	OwnerTokenFile   string  `json:"owner_token_file"`
	RouteTokenFile   string  `json:"route_token_file"`
	ControlCAFile    string  `json:"control_ca_file"`
	HTTPCAFile       string  `json:"http_ca_file"`
	Timeout          float64 `json:"control_timeout_seconds"`
}
type bearerConfig struct {
	Token     string          `json:"token"`
	TokenFile string          `json:"token_file"`
	Role      string          `json:"role"`
	Allow     []gateway.Route `json:"allow"`
}

func decodeEnv(name string, target any) error {
	value := os.Getenv(name)
	if value == "" {
		value = "{}"
	}
	d := json.NewDecoder(strings.NewReader(value))
	d.DisallowUnknownFields()
	return d.Decode(target)
}

func secretFile(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", errors.New("credential file unavailable")
	}
	return strings.TrimSpace(string(data)), nil
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "platform sessions gateway:", err)
		os.Exit(1)
	}
}

func run() error {
	var cfg instancesConfig
	var connections map[string]connection
	var bearerSettings map[string]bearerConfig
	if decodeEnv("PLATFORM_SESSIONS_INTEGRATION_CONFIG", &cfg) != nil || decodeEnv("PLATFORM_SESSIONS_PROVIDER_CONNECTIONS", &connections) != nil || decodeEnv("PLATFORM_SESSIONS_BEARERS", &bearerSettings) != nil {
		return errors.New("invalid gateway configuration")
	}
	var controls []gateway.Provider
	var channels []*gateway.ProviderConnection
	defer func() {
		for _, channel := range channels {
			_ = channel.Close()
		}
	}()
	providers := map[string]gateway.ProviderHTTPConfig{}
	for _, item := range cfg.Integrations {
		if item.Enabled != nil && !*item.Enabled {
			continue
		}
		c, ok := connections[item.ID]
		if !ok {
			return errors.New("missing provider connection")
		}
		timeout := c.Timeout
		if timeout == 0 {
			timeout = 10
		}
		control, err := gateway.ConnectProvider(&providerpb.InstanceRef{ProviderId: item.Type, InstanceId: item.ID}, c.ControlTarget, c.ControlTokenFile, c.ControlCAFile, time.Duration(timeout*float64(time.Second)))
		if err != nil {
			return err
		}
		channels = append(channels, control)
		controls = append(controls, control.Provider)
		owner, err := secretFile(c.OwnerTokenFile)
		if err != nil {
			return err
		}
		route, err := secretFile(c.RouteTokenFile)
		if err != nil {
			return err
		}
		providers[item.ID] = gateway.ProviderHTTPConfig{ID: item.ID, Origin: c.Origin, OwnerToken: owner, RouteToken: route, CAFile: c.HTTPCAFile}
	}
	if len(connections) != len(providers) {
		return errors.New("provider connections must match enabled instances")
	}
	var bearers []gateway.Bearer
	for _, b := range bearerSettings {
		if (b.Token == "") == (b.TokenFile == "") || (b.Role != "owner" && b.Role != "routes") {
			return errors.New("invalid bearer configuration")
		}
		token := b.Token
		if b.TokenFile != "" {
			var err error
			token, err = secretFile(b.TokenFile)
			if err != nil {
				return err
			}
		}
		bearers = append(bearers, gateway.Bearer{Token: token, Owner: b.Role == "owner", Routes: b.Allow})
	}
	service, err := gateway.New(controls)
	if err != nil {
		return err
	}
	managementPath := os.Getenv("PLATFORM_SESSIONS_MANAGEMENT_OPENAPI")
	if managementPath == "" {
		managementPath = "/usr/local/share/platform-sessions/management.swagger.json"
	}
	managementDoc, err := os.ReadFile(managementPath)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return errors.New("management document unavailable")
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	frontendDirectory := os.Getenv("PLATFORM_SESSIONS_FRONTEND_DIR")
	if frontendDirectory == "" {
		frontendDirectory, err = runfiles.Rlocation("ducktape/platform_sessions/frontend/dist")
		if err != nil {
			return fmt.Errorf("locate platform sessions frontend runfiles: %w", err)
		}
	}
	frontendAssets, err := frontend.Load(filepath.Clean(frontendDirectory))
	if err != nil {
		return err
	}
	handler, err := gateway.HTTPServer(ctx, service, gateway.HTTPConfig{Owner: gateway.OwnerConfig{Issuer: os.Getenv("PLATFORM_SESSIONS_ISSUER"), ClientID: os.Getenv("PLATFORM_SESSIONS_CLIENT_ID"), ClientSecret: os.Getenv("PLATFORM_SESSIONS_CLIENT_SECRET"), SessionSecret: os.Getenv("PLATFORM_SESSIONS_SESSION_SECRET"), PublicBaseURL: os.Getenv("PLATFORM_SESSIONS_PUBLIC_BASE_URL"), AllowedSubject: os.Getenv("PLATFORM_SESSIONS_ALLOWED_SUBJECT")}, Providers: providers, Bearers: bearers, Assets: frontendAssets, ManagementDocument: managementDoc})
	if err != nil {
		return err
	}
	grpcAddress := os.Getenv("PLATFORM_SESSIONS_GRPC_ADDRESS")
	if grpcAddress != "" {
		cert, err := tls.LoadX509KeyPair(os.Getenv("PLATFORM_SESSIONS_GRPC_TLS_CERT"), os.Getenv("PLATFORM_SESSIONS_GRPC_TLS_KEY"))
		if err != nil {
			return errors.New("invalid management gRPC TLS credentials")
		}
		token, err := secretFile(os.Getenv("PLATFORM_SESSIONS_GRPC_OWNER_TOKEN_FILE"))
		if err != nil {
			return err
		}
		listener, err := net.Listen("tcp", grpcAddress)
		if err != nil {
			return errors.New("could not bind management gRPC")
		}
		grpcServer := gateway.GRPCServer(service, token, grpc.Creds(credentials.NewTLS(&tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12})))
		defer grpcServer.Stop()
		go func() {
			if err := grpcServer.Serve(listener); err != nil {
				stop()
			}
		}()
	}
	port := os.Getenv("PLATFORM_SESSIONS_PORT")
	if port == "" {
		port = "8080"
	}
	server := &http.Server{Addr: ":" + port, Handler: handler, ReadHeaderTimeout: 5 * time.Second}
	done := make(chan error, 1)
	go func() { done <- server.ListenAndServe() }()
	select {
	case err := <-done:
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
		return nil
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return server.Shutdown(shutdown)
	}
}
