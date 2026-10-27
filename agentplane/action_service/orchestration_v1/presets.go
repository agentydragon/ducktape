package orchestrationv1

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"slices"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
)

// Preset is one reviewed launch preset, as the launch_sandbox and open_session fields it stands for.
type Preset struct {
	Title       string        `json:"title"`
	Launch      PresetLaunch  `json:"launch"`
	OpenSession PresetSession `json:"open_session"`
}

// PresetLaunch are launch_sandbox fields. An absent one is absent from the result too, so the fields copy into
// arguments as they are.
type PresetLaunch struct {
	Template   string   `json:"template"`
	PolicySets []string `json:"policy_sets,omitempty"`
	InboxTags  []string `json:"inbox_tags,omitempty"`
	Bootstrap  string   `json:"bootstrap,omitempty"`
}

// PresetSession are open_session fields.
type PresetSession struct {
	Spec        sessionSpec `json:"spec"`
	SetupScript string      `json:"setup_script,omitempty"`
}

// PresetList is the result of list_launch_presets.
type PresetList struct {
	Presets map[string]Preset `json:"presets"`
}

// parsePresets reads the binding's presets, refusing a field no preset has and a preset that could not fill its
// Actions' required arguments.
func parsePresets(document json.RawMessage) (map[string]Preset, error) {
	decoder := json.NewDecoder(bytes.NewReader(document))
	decoder.DisallowUnknownFields()
	var presets map[string]Preset
	if err := decoder.Decode(&presets); err != nil {
		return nil, fmt.Errorf("presets: %w", err)
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return nil, errors.New("presets must be one JSON object")
	}
	for _, name := range slices.Sorted(maps.Keys(presets)) {
		preset := presets[name]
		spec := preset.OpenSession.Spec
		if _, known := runnerpb.Harness_value[spec.Harness]; !known || spec.Harness == runnerpb.Harness_HARNESS_UNSPECIFIED.String() {
			return nil, fmt.Errorf("preset %s: open_session.spec.harness must name a harness", name)
		}
		if preset.Title == "" || preset.Launch.Template == "" || spec.Model == "" || spec.Cwd == "" {
			return nil, fmt.Errorf("preset %s: title, launch.template, open_session.spec.model and cwd are required", name)
		}
	}
	return presets, nil
}
