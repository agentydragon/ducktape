package main

import (
	"encoding/json"
	"fmt"
	"os"
)

type ServerConfig struct {
	Host  string `json:"host"`
	Port  int    `json:"port"`
	Token string `json:"token,omitempty"`
}

//go:noinline
func connectToServer(cfg ServerConfig) error {
	if cfg.Host == "" {
		return fmt.Errorf("missing required field: host")
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
