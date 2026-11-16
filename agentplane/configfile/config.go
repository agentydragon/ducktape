// Package configfile decodes strict YAML settings files.
package configfile

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"

	"github.com/go-viper/mapstructure/v2"
	"gopkg.in/yaml.v3"
)

var (
	ErrMultipleDocuments = errors.New("settings file must contain exactly one YAML document")
	ErrInvalidYAML       = errors.New("invalid YAML")
)

// MapstructureOptions preserves the few mapping differences used by existing services.
type MapstructureOptions struct {
	DecodeHooks      []mapstructure.DecodeHookFunc
	ExactFieldNames  bool
	WeaklyTypedInput bool
}

// DecodeMapstructureFile reads a settings file, decodes one YAML mapping and rejects unknown fields.
func DecodeMapstructureFile(path string, result any, options MapstructureOptions) (map[string]any, error) {
	contents, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return DecodeMapstructureYAML(contents, result, options)
}

// DecodeMapstructureYAML decodes one mapping and rejects unknown fields. The text hook also supports
// fields implementing encoding.TextUnmarshaler.
func DecodeMapstructureYAML(contents []byte, result any, options MapstructureOptions) (map[string]any, error) {
	decoder := yaml.NewDecoder(bytes.NewReader(contents))
	var settings map[string]any
	if err := decodeOne(decoder, &settings); err != nil {
		return nil, err
	}

	decodeHooks := append([]mapstructure.DecodeHookFunc{mapstructure.TextUnmarshallerHookFunc()}, options.DecodeHooks...)
	decoderConfig := &mapstructure.DecoderConfig{
		Result: result, TagName: "mapstructure", ErrorUnused: true,
		WeaklyTypedInput: options.WeaklyTypedInput,
		DecodeHook:       mapstructure.ComposeDecodeHookFunc(decodeHooks...),
	}
	if options.ExactFieldNames {
		decoderConfig.MatchName = func(mapKey, fieldName string) bool { return mapKey == fieldName }
	}
	mapDecoder, err := mapstructure.NewDecoder(decoderConfig)
	if err != nil {
		return nil, err
	}
	if err := mapDecoder.Decode(settings); err != nil {
		return nil, err
	}
	return settings, nil
}

// DecodeKnownFieldsFile reads one YAML settings file, rejecting unknown fields and extra documents.
func DecodeKnownFieldsFile(path string, result any) error {
	contents, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	decoder := yaml.NewDecoder(bytes.NewReader(contents))
	decoder.KnownFields(true)
	return decodeOne(decoder, result)
}

func decodeOne(decoder *yaml.Decoder, result any) error {
	if err := decoder.Decode(result); err != nil {
		return fmt.Errorf("%w: %w", ErrInvalidYAML, err)
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		if err != nil {
			return fmt.Errorf("%w: %w", ErrInvalidYAML, err)
		}
		return ErrMultipleDocuments
	}
	return nil
}
