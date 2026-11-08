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

type renderedEnv struct {
	Name      string `yaml:"name"`
	Value     string `yaml:"value"`
	ValueFrom struct {
		SecretKeyRef struct {
			Name string `yaml:"name"`
			Key  string `yaml:"key"`
		} `yaml:"secretKeyRef"`
	} `yaml:"valueFrom"`
}

// Read the checked-in cdk8s output so the mounted YAML, explicit Secret inputs, and strict loader
// exercise the same configuration contract as the deployed binary.
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
	var appEnv map[string]renderedEnv
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
							Name string        `yaml:"name"`
							Args []string      `yaml:"args"`
							Env  []renderedEnv `yaml:"env"`
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
				appEnv = make(map[string]renderedEnv, len(container.Env))
				for _, env := range container.Env {
					appEnv[env.Name] = env
				}
			}
		}
	}
	if config == "" || appEnv == nil {
		t.Fatal("rendered app ConfigMap or Deployment missing")
	}
	if len(args) != 0 {
		t.Fatalf("app Deployment retained settings command-line arguments: %v", args)
	}
	for name := range appEnv {
		if !strings.HasPrefix(name, "AGENTPLANE_") {
			continue
		}
		switch name {
		case "AGENTPLANE_CONFIG_FILE", "AGENTPLANE_DATABASE_URL", "AGENTPLANE_OIDC_CLIENT_ID",
			"AGENTPLANE_OIDC_CLIENT_SECRET", "AGENTPLANE_OIDC_SESSION_SECRET":
		default:
			t.Errorf("app Deployment has a per-setting Agentplane environment input %q", name)
		}
	}
	if got := appEnv["AGENTPLANE_CONFIG_FILE"].Value; got == "" {
		t.Fatal("app config path is not set by the ConfigMap mount")
	}
	for name, expected := range map[string]struct{ secret, key string }{
		"AGENTPLANE_DATABASE_URL":        {"postgres-app", "uri"},
		"AGENTPLANE_OIDC_CLIENT_ID":      {"agentplane-oidc", "client-id"},
		"AGENTPLANE_OIDC_CLIENT_SECRET":  {"agentplane-oidc", "client-secret"},
		"AGENTPLANE_OIDC_SESSION_SECRET": {"agentplane-staging-session-secret", "session-secret"},
	} {
		env, exists := appEnv[name]
		if !exists || env.ValueFrom.SecretKeyRef.Name != expected.secret || env.ValueFrom.SecretKeyRef.Key != expected.key {
			t.Errorf("%s Secret ref = %+v, want %s/%s", name, env.ValueFrom.SecretKeyRef, expected.secret, expected.key)
		}
	}
	var static map[string]any
	if err := yaml.Unmarshal([]byte(config), &static); err != nil {
		t.Fatalf("rendered app settings are not YAML: %v", err)
	}
	if _, exists := static["database_url"]; exists {
		t.Fatal("database credentials were included in the mounted static settings")
	}
	oidc, ok := static["oidc"].(map[string]any)
	if !ok || oidc["issuer"] == nil || oidc["public_base_url"] == nil {
		t.Fatalf("static OIDC settings missing from mounted config: %#v", static["oidc"])
	}
	configFile := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(configFile, []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AGENTPLANE_CONFIG_FILE", configFile)
	t.Setenv("AGENTPLANE_DATABASE_URL", "postgresql://app@postgres/agentplane")
	t.Setenv("AGENTPLANE_OIDC_CLIENT_ID", "staging-client")
	t.Setenv("AGENTPLANE_OIDC_CLIENT_SECRET", "staging-client-secret")
	t.Setenv("AGENTPLANE_OIDC_SESSION_SECRET", "staging-session-secret")
	loaded, err := loadConfig()
	if err != nil {
		t.Fatalf("rendered staging app configuration rejected: %v", err)
	}
	if loaded.OIDC == nil || loaded.OIDC.Issuer != oidc["issuer"] || len(loaded.Models.Harnesses["HARNESS_CENTRAL_SAMPLER"]) == 0 {
		t.Fatalf("rendered OIDC/model settings lost: OIDC=%+v models=%+v", loaded.OIDC, loaded.Models)
	}
}
