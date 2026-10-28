package main

import "testing"

const settingPrefix = "AGENTPLANE_SANDBOX_CONTROLLER_"

// required is the environment of a deployment that sets every setting without a default.
var required = map[string]string{
	"NAMESPACE":                 "agentplane",
	"RUNNER_GATEWAY_TARGET":     "{sandbox}-control.{namespace}.svc:8080",
	"RUNNER_GATEWAY_TOKEN_FILE": "/var/run/token",
	"RUNNER_CERTIFICATE_ISSUER": "agentplane-runner-ca",
}

func setEnvironment(t *testing.T, environment map[string]string) {
	t.Helper()
	for name, value := range environment {
		t.Setenv(settingPrefix+name, value)
	}
}

func TestEverySettingComesFromTheEnvironment(t *testing.T) {
	setEnvironment(t, required)
	setEnvironment(t, map[string]string{"METRICS_BIND_ADDRESS": ":9090", "HEALTH_PROBE_BIND_ADDRESS": ":9091"})
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	want := config{
		Namespace: "agentplane", MetricsBindAddress: ":9090", HealthProbeBindAddress: ":9091",
		RunnerGatewayTarget: "{sandbox}-control.{namespace}.svc:8080", RunnerGatewayTokenFile: "/var/run/token",
		RunnerCertificateIssuer: "agentplane-runner-ca",
	}
	if c != want {
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
