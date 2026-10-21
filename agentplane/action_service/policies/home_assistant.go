package policies

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"

	"k8s.io/apimachinery/pkg/util/sets"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

// serviceCallAction is the upstream Home Assistant MCP server's one write tool. Listing it under exact_actions would
// grant every service on every entity, `lock.unlock` and `homeassistant.restart` included.
const serviceCallAction = "ha_call_service"

var (
	// entityIDPattern is Home Assistant's identifier alphabet. A call's `entity_id` is matched against the configured
	// ids by exact string, so an id outside it, such as a comma-separated list, can never be configured as the one
	// entity a call is approved for.
	entityIDPattern = regexp.MustCompile(`^[a-z0-9_]+\.[a-z0-9_]+$`)
	servicePattern  = regexp.MustCompile(`^[a-z0-9_]+$`)
	// reviewedArguments cannot widen what a call touches: the target is checked on its own, and the rest only shape
	// the response. `ws_command` is deliberately absent: a raw websocket escape hatch past the domain, service and
	// entity.
	reviewedArguments = sets.New(
		"data", "domain", "entity_id", "result_attribute_keys", "result_fields", "return_response", "service", "verbose",
		"wait",
	)
	// retargetingKeys redirect a service call at something other than `entity_id`. Home Assistant resolves each to a
	// target set, so one inside `data` would silently widen an otherwise scoped call.
	retargetingKeys = sets.New("entity_id", "target", "area_id", "device_id", "label_id")
)

// HomeAssistantEntityControl matches a Home Assistant service call that can reach only one configured entity, with a
// service configured for it. It is an allow-list: an argument it has not reviewed, or one that could widen the target,
// is no match.
type HomeAssistantEntityControl struct {
	// Entities holds the services a call may use on each entity id.
	Entities map[string]sets.Set[string]
}

func (HomeAssistantEntityControl) Type() store.PolicyKind {
	return store.PolicyHomeAssistantEntityControl
}

func parseHomeAssistantEntityControl(spec policyapi.ActionPolicy, actions map[string]sets.Set[string]) (HomeAssistantEntityControl, error) {
	for group, names := range actions {
		if unreviewed := names.Difference(sets.New(serviceCallAction)); unreviewed.Len() > 0 {
			return HomeAssistantEntityControl{}, fmt.Errorf(
				"group %q lists %v; only %s is reviewed", group, sets.List(unreviewed), serviceCallAction,
			)
		}
	}
	if len(spec.Entities) == 0 {
		return HomeAssistantEntityControl{}, errors.New("entities lists no entity")
	}
	entities := make(map[string]sets.Set[string], len(spec.Entities))
	for entityID, services := range spec.Entities {
		if !entityIDPattern.MatchString(entityID) {
			return HomeAssistantEntityControl{}, fmt.Errorf("entity id %q does not match %s", entityID, entityIDPattern)
		}
		if len(services) == 0 {
			return HomeAssistantEntityControl{}, fmt.Errorf("entity %s allows no service", entityID)
		}
		for _, service := range services {
			if !servicePattern.MatchString(service) {
				return HomeAssistantEntityControl{}, fmt.Errorf("service %q does not match %s", service, servicePattern)
			}
		}
		entities[entityID] = sets.New(services...)
	}
	return HomeAssistantEntityControl{Entities: entities}, nil
}

func (k HomeAssistantEntityControl) evaluate(_ context.Context, _ store.ActionIdentity, arguments map[string]any, _ *Visibility) Outcome {
	if unreviewed := sets.KeySet(arguments).Difference(reviewedArguments); unreviewed.Len() > 0 {
		return NotMatched{Reason: "call carries argument(s) this policy has not reviewed: " +
			strings.Join(sets.List(unreviewed), ", ")}
	}
	entityID, ok := arguments["entity_id"].(string)
	if !ok {
		// A missing or list-valued target is how a call reaches every entity in a domain.
		return NotMatched{Reason: "auto-approval requires exactly one entity_id, given as a string"}
	}
	services, ok := k.Entities[entityID]
	if !ok {
		return NotMatched{Reason: entityID + " is not an entity this policy controls"}
	}
	entityDomain, _, _ := strings.Cut(entityID, ".")
	if domain, ok := arguments["domain"].(string); !ok || domain != entityDomain {
		return NotMatched{Reason: fmt.Sprintf(
			"domain %s does not match the entity's own domain", argumentText(arguments["domain"]),
		)}
	}
	service, ok := arguments["service"].(string)
	if !ok || !services.Has(service) {
		return NotMatched{Reason: fmt.Sprintf(
			"service %s is not allowed on %s", argumentText(arguments["service"]), entityID,
		)}
	}
	data := arguments["data"]
	if data == nil {
		data = map[string]any{}
	}
	object, ok := data.(map[string]any)
	if !ok {
		return NotMatched{Reason: "data must be an object"}
	}
	if retargeting := sets.KeySet(object).Intersection(retargetingKeys); retargeting.Len() > 0 {
		return NotMatched{Reason: "data would retarget the call via: " + strings.Join(sets.List(retargeting), ", ")}
	}
	return Matched{Explanation: fmt.Sprintf(
		"%s.%s targets only %s, which this policy controls", entityDomain, service, entityID,
	)}
}

// argumentText is an argument as JSON text, `null` when absent.
func argumentText(value any) string {
	text, err := json.Marshal(value)
	if err != nil {
		// Only a value that was never JSON fails to encode; the reason still names it.
		return fmt.Sprintf("%v", value)
	}
	return string(text)
}
