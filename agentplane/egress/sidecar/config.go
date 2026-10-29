package main

import (
	"fmt"
	"os"

	"buf.build/go/protovalidate"
	"buf.build/go/protoyaml"
	"github.com/agentydragon/ducktape/agentplane/egress/sidecar/configpb"
	"go.yaml.in/yaml/v3"
	"google.golang.org/protobuf/proto"
)

const configFileEnv = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

func loadConfig(getenv func(string) string) (*configpb.Config, error) {
	path := getenv(configFileEnv)
	if path == "" {
		return nil, fmt.Errorf("%s must name the YAML settings file", configFileEnv)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	// protoyaml v0.7 decodes through yaml.Node without rejecting duplicate mapping keys.
	// Preserve the settings loader's refusal of ambiguous documents.
	if err := yaml.Unmarshal(data, new(any)); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	c := &configpb.Config{}
	if err := (protoyaml.UnmarshalOptions{Path: path}).Unmarshal(data, c); err != nil {
		return nil, err
	}
	if c.ProxyPort == nil {
		c.ProxyPort = proto.Int32(8888)
	}
	if c.ListenAddress == nil {
		c.ListenAddress = proto.String("127.0.0.1:3128")
	}
	if c.ReadinessListenAddress == nil {
		c.ReadinessListenAddress = proto.String("0.0.0.0:3129")
	}
	if err := protovalidate.Validate(c); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return c, nil
}
