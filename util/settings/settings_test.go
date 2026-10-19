package settings

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

const prefix = "TEST_SERVICE_"

type model struct {
	Name   string `yaml:"name"`
	Budget int    `yaml:"budget"`
}

type sample struct {
	DatabaseURL string            `yaml:"database_url"`
	ListenPort  int               `yaml:"listen_port"`
	Verbose     bool              `yaml:"verbose"`
	Namespaces  []string          `env:"-" yaml:"namespaces"`
	Models      []model           `env:"-" yaml:"models"`
	Levels      map[string]string `env:"-" yaml:"levels"`
	Backend     string            `env:"UPSTREAM" yaml:"backend_url"`
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
	return Load(prefix, defaults(), environ)
}

func TestTheEnvironmentOverridesTheFileWhichOverridesTheDefaults(t *testing.T) {
	path := writeFile(t, `listen_port: 1111
verbose: true
namespaces: [from-file]
models:
  - {name: from-file, budget: 1}
backend_url: http://file.test
levels: {root: DEBUG}
`)
	got, err := load(
		prefix+"CONFIG_FILE="+path, prefix+"LISTEN_PORT=2222", prefix+"VERBOSE=false",
	)
	if err != nil {
		t.Fatal(err)
	}
	want := sample{
		ListenPort: 2222, Verbose: false, Namespaces: []string{"from-file"},
		Models: []model{{Name: "from-file", Budget: 1}}, Levels: map[string]string{"root": "DEBUG"},
		Backend: "http://file.test",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v, want %+v", got, want)
	}
}

func TestAFieldTaggedWithItsEnvironmentNameIsReadFromIt(t *testing.T) {
	got, err := load(prefix + "UPSTREAM=http://env.test")
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
	_, err := load(prefix + "LISTEN_PORT=" + secret)
	if err == nil || strings.Contains(err.Error(), secret) {
		t.Fatalf("missing or unsafe parse error: %v", err)
	}
}

func TestCollectionsAreFileOnly(t *testing.T) {
	path := writeFile(t, "namespaces: [from-file]\nmodels: [{name: from-file, budget: 1}]\nlevels: {root: DEBUG}\n")
	for _, setting := range []string{
		`NAMESPACES=["from-env"]`, `NAMESPACES=one,two`,
		`MODELS=[{"name":"from-env","budget":3}]`, `LEVELS={"root":"INFO"}`,
	} {
		if _, err := load(prefix+"CONFIG_FILE="+path, prefix+setting); err == nil {
			t.Errorf("accepted collection environment override %s", setting)
		}
	}
}
