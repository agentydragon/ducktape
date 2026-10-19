// Package settings loads a service's settings from a YAML file and environment variables.
//
// A settings struct has one exported field per setting, each tagged `yaml:"snake_case_key"`. The
// YAML key is read from the file named by <prefix>CONFIG_FILE. The environment overrides the file,
// which overrides the defaults the caller passes in. Lists and maps are file-only and carry
// `env:"-"` tags.
// Scalar environment names default to the Go field name in upper snake case; an `env:"KEY"`
// tag overrides that name independently of the YAML key.
package settings

import (
	"bytes"
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

// Load returns defaults overlaid by the settings file, then the environment. It refuses a file key
// the struct has no setting for and an environment variable with the prefix that names no setting.
// Its errors name settings and never carry the values given for them.
func Load[T any](prefix string, defaults T, environ []string) (T, error) {
	var zero T
	loaded := defaults
	settingsType := reflect.TypeFor[T]()
	if settingsType.Kind() != reflect.Struct {
		return zero, fmt.Errorf("settings type %s is not a struct", settingsType)
	}
	environment := env.ToMap(environ)
	envOptions := env.Options{
		Environment: environment, Prefix: prefix, UseFieldNameByDefault: true,
	}
	fields, err := env.GetFieldParamsWithOptions(&loaded, envOptions)
	if err != nil {
		return zero, err
	}
	configFileEnv := prefix + configFileSuffix
	known := map[string]bool{configFileEnv: true}
	for _, field := range fields {
		known[field.Key] = true
	}
	for _, name := range slices.Sorted(maps.Keys(environment)) {
		if strings.HasPrefix(name, prefix) && !known[name] {
			return zero, fmt.Errorf("unknown setting %s", name)
		}
	}
	if path := environment[configFileEnv]; path != "" {
		if err := readFile(path, configFileEnv, &loaded); err != nil {
			return zero, err
		}
	}
	if err := env.ParseWithOptions(&loaded, envOptions); err != nil {
		return zero, parseFailure(settingsType, err)
	}
	return loaded, nil
}

// readFile decodes a file that must exist and hold exactly one document of known keys.
func readFile(path, configFileEnv string, target any) error {
	contents, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("%s=%s is not a readable file", configFileEnv, path)
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
	return fmt.Errorf("settings must be a value of their type: %s", strings.Join(failed, ", "))
}
