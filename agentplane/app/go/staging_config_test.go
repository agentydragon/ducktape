package main

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/bazelbuild/rules_go/go/runfiles"
	"gopkg.in/yaml.v3"
)

var stagingConfigRlocation string

// Read the checked-in cdk8s output so added staging keys and CLI flags exercise
// the same strict loader as the deployed binary, without copying its catalog.
func TestRenderedStagingConfiguration(t *testing.T) {
	path, err := runfiles.Rlocation(stagingConfigRlocation)
	if err != nil {
		t.Fatal(err)
	}
	file, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	decoder := yaml.NewDecoder(file)
	var config string
	var args []string
	for {
		var document struct {
			Kind     string `yaml:"kind"`
			Metadata struct {
				Name string `yaml:"name"`
			} `yaml:"metadata"`
			Data map[string]string `yaml:"data"`
			Spec struct {
				Template struct {
					Spec struct {
						Containers []struct {
							Name string   `yaml:"name"`
							Args []string `yaml:"args"`
							Env  []struct {
								Name  string `yaml:"name"`
								Value string `yaml:"value"`
							} `yaml:"env"`
						} `yaml:"containers"`
					} `yaml:"spec"`
				} `yaml:"template"`
			} `yaml:"spec"`
		}
		if err := decoder.Decode(&document); errors.Is(err, io.EOF) {
			break
		} else if err != nil {
			t.Fatal(err)
		}
		if document.Kind == "ConfigMap" && document.Metadata.Name == "app-config" {
			config = document.Data["config.yaml"]
		}
		if document.Kind == "Deployment" && document.Metadata.Name == "app" {
			for _, container := range document.Spec.Template.Spec.Containers {
				if container.Name != "app" {
					continue
				}
				args = container.Args
				for _, env := range container.Env {
					if strings.HasPrefix(env.Name, "AGENTPLANE_") && env.Value != "" {
						t.Setenv(env.Name, env.Value)
					}
				}
			}
		}
	}
	if config == "" || len(args) == 0 {
		t.Fatal("rendered app ConfigMap or Deployment missing")
	}
	configPath := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(configPath, []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AGENTPLANE_CONFIG_FILE", configPath)
	// This value is supplied by a Secret in the Deployment; loading never connects.
	t.Setenv("AGENTPLANE_DATABASE_URL", "postgresql://app@postgres/agentplane")
	loaded, err := loadConfigWithArgs(args)
	if err != nil {
		t.Fatalf("rendered staging app configuration rejected: %v", err)
	}
	if len(loaded.Models.Harnesses["HARNESS_CENTRAL_SAMPLER"]) == 0 {
		t.Fatal("rendered central sampler model offerings lost")
	}
}
