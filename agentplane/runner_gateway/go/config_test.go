package main

import "testing"

func TestUnifiedGatewayConfigKeepsAuthAndWakePoliciesSeparate(t *testing.T) {
	env := map[string]string{
		"AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL": "postgres://queue",
		"AGENTPLANE_RUNNER_GATEWAY_POD_NAME":                "runner-gateway-0",
		"AGENTPLANE_RUNNER_GATEWAY_POD_IP":                  "10.0.0.5",
	}
	getenv := func(name string) string { return env[name] }
	args := []string{
		"--sandbox-namespace=agentplane", "--runner-token-file=/var/run/token",
		"--dispatcher-audience=session-dispatcher", "--gateway-audience=agentplane-runner-gateway",
		"--runner-audience=agentplane-runner-control",
		"--dispatcher-caller=agentplane/app", "--dispatcher-caller=agentplane/notifications",
		"--wake-caller=agentplane/app", "--gateway-caller=agentplane/runner-gateway",
		"--sandbox-controller-account=agentplane/sandbox-controller",
	}
	c, err := loadConfig(args, getenv)
	if err != nil {
		t.Fatal(err)
	}
	if len(c.DispatcherCallers) != 2 || len(c.WakeCallers) != 1 || len(c.GatewayCallers) != 1 ||
		c.DispatcherAudience == c.GatewayAudience || c.ControllerAccount.Name != "sandbox-controller" {
		t.Fatalf("policy boundaries were merged: %+v", c)
	}
	invalid := append(append([]string{}, args...), "--wake-caller=agentplane/unknown")
	if _, err := loadConfig(invalid, getenv); err == nil {
		t.Fatal("unauthorized wake caller accepted")
	}
}
