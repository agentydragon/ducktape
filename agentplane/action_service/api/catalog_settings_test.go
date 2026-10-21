package api

import (
	"encoding/json"
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

// offered is the catalog of document with each group in offers offering its Actions, as its executor would report
// them; a group without Actions stays as configured.
func offered(t *testing.T, document string, offers map[string]offer) *catalog.Catalog {
	t.Helper()
	groups := newCatalog(t, document)
	for key, offering := range offers {
		groups.Update(key, func(group *catalog.Group) {
			group.Available, group.Actions = !offering.unavailable, offering.actions
		})
	}
	return groups
}

type offer struct {
	actions     map[string]catalog.ActionDefinition
	unavailable bool
}

// described is an Action with only a description, and the empty input schema of an Action that takes any arguments.
func described(description string) catalog.ActionDefinition {
	return catalog.ActionDefinition{Description: description, InputSchema: json.RawMessage(`{}`)}
}
