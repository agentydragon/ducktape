package main

import (
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
)

func TestEnvironmentServiceDelegationAllowsReadsAndResumeWithoutConnectionProvenance(t *testing.T) {
	base := `environment_service:
  policy_namespace: agentplane
  audience: session-dispatcher
  rpc:
    listen_address: ":8082"
    tls: {cert_file: /tmp/server-cert, key_file: /tmp/server-key}
    auth: [{mode: spiffe, ca_file: /tmp/ca}]
  service_delegation:
    actors: [{spiffe_id: "spiffe://agentplane/actions"}]
    methods: [/agentplane.environments.v1.EnvironmentService/ListEnvironments, /agentplane.environments.v1.EnvironmentService/ResumeEnvironment]
`
	t.Setenv(configFileEnv, writeSettings(t, validSettings+base))
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.EnvironmentService.ServiceDelegation == nil || len(c.EnvironmentService.ServiceDelegation.Actors) != 1 ||
		c.EnvironmentService.ServiceDelegation.Actors[0].SPIFFEID != peertls.ActionsURI {
		t.Fatalf("EnvironmentService service delegation = %+v", c.EnvironmentService.ServiceDelegation)
	}

	for name, settings := range map[string]string{
		"mutation method":       strings.Replace(base, "/agentplane.environments.v1.EnvironmentService/ListEnvironments", "/agentplane.environments.v1.EnvironmentService/CreateEnvironment", 1),
		"Connection provenance": strings.Replace(base, `spiffe_id: "spiffe://agentplane/actions"`, `spiffe_id: "spiffe://agentplane/actions", connection_provenance: true`, 1),
		"Identity URI actor":    strings.Replace(base, "spiffe://agentplane/actions", "spiffe://agentplane/test/sandbox/credential-id", 1),
	} {
		t.Run(name, func(t *testing.T) {
			t.Setenv(configFileEnv, writeSettings(t, validSettings+settings))
			if _, err := loadConfig(); err == nil {
				t.Fatal("invalid EnvironmentService delegation was accepted")
			}
		})
	}
}
