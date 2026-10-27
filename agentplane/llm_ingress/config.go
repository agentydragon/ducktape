package main

import (
	"errors"
	"os"
	"strconv"
	"strings"

	"buf.build/go/protovalidate"
	"buf.build/go/protoyaml"
	"github.com/agentydragon/ducktape/agentplane/llm_ingress/configpb"
	"go.yaml.in/yaml/v3"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

const (
	settingPrefix = "AGENTPLANE_LLM_INGRESS_"
	configFileEnv = settingPrefix + "CONFIG_FILE"
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
	if path := os.Getenv(configFileEnv); path != "" {
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, errors.New("could not read ingress settings file")
		}
		// Parse/validation errors can contain Secret values; only this boundary reports them.
		// protoyaml does not reject duplicate YAML keys itself.
		if yaml.Unmarshal(data, new(any)) != nil || (protoyaml.UnmarshalOptions{}).Unmarshal(data, c) != nil {
			return nil, errors.New("invalid ingress settings")
		}
	}
	// Only top-level scalars accept environment overrides; lists, maps and attribution
	// remain file-only. Empty environment values are ignored, matching Viper's contract.
	message := c.ProtoReflect()
	fields := message.Descriptor().Fields()
	for i := 0; i < fields.Len(); i++ {
		field := fields.Get(i)
		if field.IsList() || field.IsMap() || field.Kind() == protoreflect.MessageKind {
			continue
		}
		value := os.Getenv(settingPrefix + strings.ToUpper(string(field.Name())))
		if value == "" {
			continue
		}
		switch field.Kind() {
		case protoreflect.StringKind:
			message.Set(field, protoreflect.ValueOfString(value))
		case protoreflect.BoolKind:
			parsed, err := strconv.ParseBool(value)
			if err != nil {
				return nil, errors.New("invalid ingress environment setting")
			}
			message.Set(field, protoreflect.ValueOfBool(parsed))
		case protoreflect.Int32Kind:
			parsed, err := strconv.ParseInt(value, 10, 32)
			if err != nil {
				return nil, errors.New("invalid ingress environment setting")
			}
			message.Set(field, protoreflect.ValueOfInt32(int32(parsed)))
		default:
			return nil, errors.New("unsupported ingress environment setting")
		}
	}
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
