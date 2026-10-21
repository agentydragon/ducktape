package api

import (
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"gopkg.in/yaml.v3"
)

// parseCatalog builds the catalog of an `action_groups` setting, decoded as the service decodes its settings file.
func parseCatalog(document string) (*catalog.Catalog, error) {
	var settings catalog.Settings
	decoder := yaml.NewDecoder(strings.NewReader(document))
	decoder.KnownFields(true)
	if err := decoder.Decode(&settings); err != nil {
		return nil, err
	}
	return catalog.New(settings)
}

func newCatalog(t *testing.T, document string) *catalog.Catalog {
	t.Helper()
	groups, err := parseCatalog(document)
	if err != nil {
		t.Fatal(err)
	}
	return groups
}
