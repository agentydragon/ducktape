package main

import (
	"errors"
	"os"

	"buf.build/go/protovalidate"
	"buf.build/go/protoyaml"
	"github.com/agentydragon/ducktape/agentplane/llm_ingress/configpb"
	"go.yaml.in/yaml/v3"
	"google.golang.org/protobuf/proto"
)

const (
	configFileEnv = "AGENTPLANE_LLM_INGRESS_CONFIG_FILE"
	litellmKeyEnv = "AGENTPLANE_LLM_INGRESS_LITELLM_KEY"
)

func defaultAttribution() *configpb.AttributionConfig {
	header := &configpb.AttributionJsonHeader{Name: "x-litellm-spend-logs-metadata"}
	for _, source := range []configpb.VerifiedClaim{configpb.VerifiedClaim_namespace, configpb.VerifiedClaim_service_account, configpb.VerifiedClaim_pod_name, configpb.VerifiedClaim_pod_uid} {
		header.Fields = append(header.Fields, &configpb.AttributionField{Key: "agentplane." + source.String(), Source: source})
	}
	return &configpb.AttributionConfig{JsonHeaders: []*configpb.AttributionJsonHeader{header}}
}

func loadConfig() (*configpb.Config, error) {
	c := &configpb.Config{}
	data, err := os.ReadFile(os.Getenv(configFileEnv))
	if err != nil {
		return nil, errors.New("could not read ingress settings file")
	}
	// Parse/validation errors can contain Secret values; only this boundary reports them.
	// protoyaml does not reject duplicate YAML keys itself.
	if yaml.Unmarshal(data, new(any)) != nil || (protoyaml.UnmarshalOptions{}).Unmarshal(data, c) != nil {
		return nil, errors.New("invalid ingress settings")
	}
	c.LitellmKey = os.Getenv(litellmKeyEnv)
	if c.Host == nil {
		c.Host = proto.String("0.0.0.0")
	}
	if c.Port == nil {
		c.Port = proto.Int32(8080)
	}
	if c.Attribution == nil {
		c.Attribution = defaultAttribution()
	}
	if protovalidate.Validate(c) != nil {
		return nil, errors.New("invalid ingress settings")
	}
	return c, nil
}
