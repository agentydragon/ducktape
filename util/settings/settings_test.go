package settings

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

const prefix = "TEST_SERVICE_"

type model struct {
	Name   string `json:"name" yaml:"name"`
	Budget int    `json:"budget" yaml:"budget"`
}

type sample struct {
	DatabaseURL string            `yaml:"database_url"`
	ListenPort  int               `yaml:"listen_port"`
	Verbose     bool              `yaml:"verbose"`
	Namespaces  []string          `yaml:"namespaces"`
	Models      []model           `yaml:"models"`
	Levels      map[string]string `yaml:"levels"`
	Backend     string            `env:"BACKEND_URL" yaml:"backend_url"`
}

func defaults() sample {
	return sample{ListenPort: 8080, Levels: map[string]string{"root": "INFO"}}
}

func writeFile(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "settings.yaml")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func load(environ ...string) (sample, error) {
	return Load(Options{Prefix: prefix}, defaults(), environ)
}

func TestTheEnvironmentOverridesTheFileWhichOverridesTheDefaults(t *testing.T) {
	path := writeFile(t, `listen_port: 1111
verbose: true
namespaces: [from-file]
models:
  - {name: from-file, budget: 1}
backend_url: http://file.test
`)
	got, err := load(
		prefix+"CONFIG_FILE="+path, prefix+"LISTEN_PORT=2222", prefix+"VERBOSE=false",
		prefix+`NAMESPACES=["from-env"]`, prefix+`LEVELS={"root":"DEBUG"}`,
		prefix+`MODELS=[{"name":"from-env","budget":3}]`,
	)
	if err != nil {
		t.Fatal(err)
	}
	want := sample{
		ListenPort: 2222, Verbose: false, Namespaces: []string{"from-env"},
		Models: []model{{Name: "from-env", Budget: 3}}, Levels: map[string]string{"root": "DEBUG"},
		Backend: "http://file.test",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v, want %+v", got, want)
	}
}

func TestAFieldTaggedWithItsEnvironmentNameIsReadFromIt(t *testing.T) {
	got, err := load(prefix + "BACKEND_URL=http://env.test")
	if err != nil || got.Backend != "http://env.test" {
		t.Fatalf("got %+v, %v", got, err)
	}
}

func TestWhatTheStructHasNoSettingForIsRefused(t *testing.T) {
	path := writeFile(t, "listen_port: 1\nconfdir: /tmp\n")
	for name, attempt := range map[string]func() (sample, error){
		"a file key": func() (sample, error) { return load(prefix + "CONFIG_FILE=" + path) },
		"a variable": func() (sample, error) { return load(prefix + "CONFDIR=/tmp") },
	} {
		if _, err := attempt(); err == nil {
			t.Errorf("%s the struct has no setting for was accepted", name)
		}
	}
	if _, err := load("OTHER_SERVICE_CONFDIR=/tmp"); err != nil {
		t.Errorf("a variable outside the prefix was refused: %v", err)
	}
}

func TestASettingsFileThatIsNamedMustExistAndHoldOneDocument(t *testing.T) {
	if _, err := load(prefix + "CONFIG_FILE=/nonexistent/settings.yaml"); err == nil {
		t.Error("a missing settings file ran on defaults")
	}
	path := writeFile(t, "listen_port: 1\n---\nlisten_port: 2\n")
	if _, err := load(prefix + "CONFIG_FILE=" + path); err == nil {
		t.Error("a settings file with two documents was accepted")
	}
}

func TestErrorsNeverCarryTheValuesGiven(t *testing.T) {
	const secret = "never-print-this"
	for name, attempt := range map[string]func() (sample, error){
		"scalar":        func() (sample, error) { return load(prefix + "LISTEN_PORT=" + secret) },
		"list":          func() (sample, error) { return load(prefix + "NAMESPACES=" + secret) },
		"unknown field": func() (sample, error) { return load(prefix + `MODELS=[{"name":"x","extra":"` + secret + `"}]`) },
	} {
		_, err := attempt()
		if err == nil {
			t.Errorf("%s: accepted", name)
		} else if strings.Contains(err.Error(), secret) {
			t.Errorf("%s: the error carries the value: %v", name, err)
		}
	}
}

func TestTheFileIsVettedBeforeItIsDecoded(t *testing.T) {
	path := writeFile(t, "listen_port: 1\n")
	refusal := errors.New("refused by the check")
	var seen string
	_, err := Load(Options{Prefix: prefix, CheckFile: func(contents []byte) error {
		seen = string(contents)
		return refusal
	}}, defaults(), []string{prefix + "CONFIG_FILE=" + path})
	if !errors.Is(err, refusal) || seen != "listen_port: 1\n" {
		t.Fatalf("got %v after checking %q", err, seen)
	}
}

func TestAStructWhoseEnvironmentNamesAreNotItsKeysIsRefused(t *testing.T) {
	type misnamed struct {
		BackendURL string `yaml:"backend_url"`
		LiteLLMURL string `yaml:"litellm_url"`
	}
	if _, err := Load(Options{Prefix: prefix}, misnamed{}, nil); err == nil {
		t.Fatal("LiteLLMURL was accepted for litellm_url")
	}
}
