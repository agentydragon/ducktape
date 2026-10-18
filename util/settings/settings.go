// Package settings loads a service's settings from a YAML file and environment variables.
//
// A settings struct has one exported field per setting, each tagged `yaml:"snake_case_key"`. The
// setting is that key in the file named by <prefix>CONFIG_FILE and the environment variable
// <prefix><KEY UPPER-CASED>. The environment overrides the file, which overrides the defaults the
// caller passes in. A field is a scalar, or a list or map that the environment gives as JSON (a
// struct inside one carries json tags). A field whose Go name does not spell its key, such as
// LiteLLMURL for litellm_url, carries an `env:"KEY"` tag; Load refuses a struct whose environment
// names and keys differ.
package settings

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"os"
	"reflect"
	"slices"
	"strings"

	"github.com/caarlos0/env/v11"
	"gopkg.in/yaml.v3"
)

const configFileSuffix = "CONFIG_FILE"

// Options says where a service's settings come from.
type Options struct {
	// Prefix starts every environment variable the service reads, and ends in an underscore.
	Prefix string
	// CheckFile, when set, vets the settings file's contents before they are decoded.
	CheckFile func(contents []byte) error
}

// Load returns defaults overlaid by the settings file, then the environment. It refuses a file key
// the struct has no setting for and an environment variable with the prefix that names no setting.
// Its errors name settings and never carry the values given for them.
func Load[T any](options Options, defaults T, environ []string) (T, error) {
	var zero T
	loaded := defaults
	settingsType := reflect.TypeFor[T]()
	if settingsType.Kind() != reflect.Struct {
		return zero, fmt.Errorf("settings type %s is not a struct", settingsType)
	}
	environment := make(map[string]string, len(environ))
	for _, item := range environ {
		if name, value, found := strings.Cut(item, "="); found {
			environment[name] = value
		}
	}
	envOptions := env.Options{
		Environment: environment, Prefix: options.Prefix, UseFieldNameByDefault: true,
		FuncMap: jsonParsers(settingsType),
	}
	fields, err := env.GetFieldParamsWithOptions(&loaded, envOptions)
	if err != nil {
		return zero, err
	}
	if err := checkNames(settingsType, fields); err != nil {
		return zero, err
	}
	configFileEnv := options.Prefix + configFileSuffix
	known := map[string]bool{configFileEnv: true}
	for _, field := range fields {
		known[field.Key] = true
	}
	for _, name := range slices.Sorted(maps.Keys(environment)) {
		if strings.HasPrefix(name, options.Prefix) && !known[name] {
			return zero, fmt.Errorf("unknown setting %s", name)
		}
	}
	if path := environment[configFileEnv]; path != "" {
		if err := readFile(path, configFileEnv, options.CheckFile, &loaded); err != nil {
			return zero, err
		}
	}
	if err := env.ParseWithOptions(&loaded, envOptions); err != nil {
		return zero, parseFailure(settingsType, err)
	}
	return loaded, nil
}

// jsonParsers makes each list and map field read as strict JSON.
func jsonParsers(settingsType reflect.Type) map[reflect.Type]env.ParserFunc {
	parsers := map[reflect.Type]env.ParserFunc{}
	for field := range settingsType.Fields() {
		if kind := field.Type.Kind(); kind == reflect.Slice || kind == reflect.Map {
			parsers[field.Type] = parseJSONAs(field.Type)
		}
	}
	return parsers
}

func parseJSONAs(valueType reflect.Type) env.ParserFunc {
	return func(text string) (any, error) {
		value := reflect.New(valueType)
		decoder := json.NewDecoder(strings.NewReader(text))
		decoder.DisallowUnknownFields()
		if decoder.Decode(value.Interface()) != nil || !errors.Is(decoder.Decode(new(any)), io.EOF) {
			return nil, errors.New("not a JSON value of its type")
		}
		return value.Elem().Interface(), nil
	}
}

// checkNames refuses a struct whose environment names are not its YAML keys upper-cased: the file
// and the environment would otherwise name one setting two ways.
func checkNames(settingsType reflect.Type, fields []env.FieldParams) error {
	want := map[string]bool{}
	for field := range settingsType.Fields() {
		if !field.IsExported() {
			continue
		}
		key, _, _ := strings.Cut(field.Tag.Get("yaml"), ",")
		if key == "" || key == "-" {
			return fmt.Errorf("settings field %s.%s has no yaml key", settingsType, field.Name)
		}
		want[strings.ToUpper(key)] = true
	}
	got := map[string]bool{}
	for _, field := range fields {
		got[field.OwnKey] = true
	}
	if !maps.Equal(want, got) {
		return fmt.Errorf("settings %s: the yaml keys upper-cased are %v but the environment names are %v",
			settingsType, slices.Sorted(maps.Keys(want)), slices.Sorted(maps.Keys(got)))
	}
	return nil
}

// readFile decodes a file that must exist and hold exactly one document of known keys.
func readFile(path, configFileEnv string, check func([]byte) error, target any) error {
	contents, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("%s=%s is not a readable file", configFileEnv, path)
	}
	if check != nil {
		if err := check(contents); err != nil {
			return fmt.Errorf("invalid settings file %s: %w", path, err)
		}
	}
	decoder := yaml.NewDecoder(bytes.NewReader(contents))
	decoder.KnownFields(true)
	if err := decoder.Decode(target); err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("invalid settings file %s: %w", path, err)
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return fmt.Errorf("settings file %s must hold one document", path)
	}
	return nil
}

// parseFailure names the settings that failed to parse and never carries their text: a database
// URL holds a password.
func parseFailure(settingsType reflect.Type, err error) error {
	var aggregate env.AggregateError
	if !errors.As(err, &aggregate) {
		return err
	}
	var failed []string
	for _, failure := range aggregate.Errors {
		var parseError env.ParseError
		if !errors.As(failure, &parseError) {
			return failure
		}
		field, _ := settingsType.FieldByName(parseError.Name)
		failed = append(failed, field.Tag.Get("yaml"))
	}
	return fmt.Errorf("settings must be a value of their type, a list or map as JSON: %s", strings.Join(failed, ", "))
}
