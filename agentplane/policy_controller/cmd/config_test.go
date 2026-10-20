package main

import (
	"slices"
	"testing"
)

const settingPrefix = "AGENTPLANE_POLICY_CONTROLLER_"

// required is the environment of a deployment that sets every setting without a default.
var required = map[string]string{
	"POLICY_NAMESPACE":   "agentplane",
	"SUBJECT_NAMESPACES": "agentplane",
}

func setEnvironment(t *testing.T, environment map[string]string) {
	t.Helper()
	for name, value := range environment {
		t.Setenv(settingPrefix+name, value)
	}
}

func TestEverySettingComesFromTheEnvironment(t *testing.T) {
	setEnvironment(t, map[string]string{
		"POLICY_NAMESPACE": "policies", "SUBJECT_NAMESPACES": "agents,tools",
		"METRICS_BIND_ADDRESS": ":9090", "HEALTH_PROBE_BIND_ADDRESS": ":9091",
	})
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.PolicyNamespace != "policies" || !slices.Equal(c.SubjectNamespaces, []string{"agents", "tools"}) ||
		c.MetricsBindAddress != ":9090" || c.HealthProbeBindAddress != ":9091" {
		t.Fatalf("settings were not read: %+v", c)
	}
}

func TestBindAddressesDefaultWhenUnset(t *testing.T) {
	setEnvironment(t, required)
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.MetricsBindAddress != ":8080" || c.HealthProbeBindAddress != ":8081" {
		t.Fatalf("defaults were lost: %+v", c)
	}
}

func TestEachRequiredSettingIsRequired(t *testing.T) {
	for omitted := range required {
		t.Run(omitted, func(t *testing.T) {
			for name, value := range required {
				if name != omitted {
					t.Setenv(settingPrefix+name, value)
				}
			}
			if _, err := loadConfig(); err == nil {
				t.Fatalf("started without %s", omitted)
			}
		})
	}
}

func TestANamespaceNameThatCannotExistIsRefused(t *testing.T) {
	for _, subjects := range []string{"agents,", "agents, tools", "Agents"} {
		t.Run(subjects, func(t *testing.T) {
			setEnvironment(t, required)
			t.Setenv(settingPrefix+"SUBJECT_NAMESPACES", subjects)
			if _, err := loadConfig(); err == nil {
				t.Fatal("started with a namespace name that cannot exist")
			}
		})
	}
}
