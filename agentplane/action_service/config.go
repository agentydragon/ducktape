package main

import (
	"errors"
	"os"

	"github.com/spf13/viper"
)

const settingPrefix = "AGENTPLANE_ACTIONS_"

// config is the Go Action Service's deployment contract: each setting is a YAML key of the file named by
// AGENTPLANE_ACTIONS_CONFIG_FILE, and each also names an AGENTPLANE_ACTIONS_* environment variable that overrides
// it. Unknown keys in the file are refused. A key has the name and meaning it has in the Python service's
// settings (settings.py), which serve the same deployment.
//
// The database URL is a Secret, so a deployment supplies it as an environment variable.
type config struct {
	DatabaseURL string `mapstructure:"database_url"`
	Host        string `mapstructure:"host"`
	Port        int    `mapstructure:"port"`
}

func defaultConfig() config { return config{Host: "0.0.0.0", Port: 8080} }

func loadConfig() (config, error) {
	c := defaultConfig()
	// Without ExperimentalBindStruct, Unmarshal reads an environment variable only for a bound key.
	settings := viper.NewWithOptions(viper.ExperimentalBindStruct())
	settings.SetEnvPrefix("AGENTPLANE_ACTIONS")
	settings.AutomaticEnv()
	if path := os.Getenv(settingPrefix + "CONFIG_FILE"); path != "" {
		settings.SetConfigFile(path)
		settings.SetConfigType("yaml")
		if err := settings.ReadInConfig(); err != nil {
			return config{}, errors.New("could not read Action Service settings file")
		}
	}
	// The errors name no value: a setting can be a Secret.
	if err := settings.UnmarshalExact(&c); err != nil {
		return config{}, errors.New("invalid Action Service settings")
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

func (c *config) validate() error {
	if c.DatabaseURL == "" {
		return errors.New("database_url is required")
	}
	if c.Host == "" {
		return errors.New("host is required")
	}
	if c.Port < 1 || c.Port > 65535 {
		return errors.New("port must be between 1 and 65535")
	}
	return nil
}
