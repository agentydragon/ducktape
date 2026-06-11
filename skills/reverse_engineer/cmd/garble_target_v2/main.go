package main

import (
	"encoding/json"
	"fmt"
	"os"
)

// v2: same as v1, but adds validateConfig() — new function, not in v1.
// connectToServer now calls validateConfig before returning the connection error.
// All original strings are preserved so string-anchored matching works across versions.

type ServerConfig struct {
	Host  string `json:"host"`
	Port  int    `json:"port"`
	Token string `json:"token,omitempty"`
}

//go:noinline
func validateConfig(cfg ServerConfig) error {
	if cfg.Port < 1 || cfg.Port > 65535 {
		return fmt.Errorf("invalid port: must be between 1 and 65535")
	}
	return nil
}

//go:noinline
func connectToServer(cfg ServerConfig) error {
	if cfg.Host == "" {
		return fmt.Errorf("missing required field: host")
	}
	if err := validateConfig(cfg); err != nil {
		return err
	}
	return fmt.Errorf("connection refused: server not accepting connections")
}

//go:noinline
func loadConfig(path string) (ServerConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return ServerConfig{}, fmt.Errorf("failed to read config file: %w", err)
	}
	var cfg ServerConfig
	if err := json.Unmarshal(data, &cfg); err != nil {
		return ServerConfig{}, fmt.Errorf("failed to parse config: %w", err)
	}
	return cfg, nil
}

func main() {
	cfg, err := loadConfig("config.json")
	if err != nil {
		fmt.Fprintf(os.Stderr, "startup failed: %v\n", err)
		os.Exit(1)
	}
	if err := connectToServer(cfg); err != nil {
		fmt.Fprintf(os.Stderr, "connection error: %v\n", err)
		os.Exit(1)
	}
}
