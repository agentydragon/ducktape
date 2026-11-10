// Package settings shares deployment setting merges used by Agentplane services.
package settings

import (
	"os"
	"strings"

	"github.com/spf13/viper"
)

// SetJSONLeaves preserves nested environment overrides of a JSON object. Viper's Set has higher precedence than
// AutomaticEnv, so a parent map must be set along with each leaf's effective value.
func SetJSONLeaves(settings *viper.Viper, prefix string, values map[string]any, envPrefix string) {
	settings.Set(prefix, values)
	var leaves func(string, map[string]any)
	leaves = func(parent string, object map[string]any) {
		for key, value := range object {
			path := parent + "." + key
			if nested, ok := value.(map[string]any); ok {
				leaves(path, nested)
				continue
			}
			env := envPrefix + strings.ToUpper(strings.ReplaceAll(path, ".", "__"))
			if override, present := os.LookupEnv(env); present && override != "" {
				value = override
			}
			settings.Set(path, value)
		}
	}
	leaves(prefix, values)
}
