import type { ActionPresentation } from "../presentation";
import { haAction } from "./secondary_common";

export const homeAssistantOrganizationRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "ha_config_get_automation",
    haAction({
      title: "Read automation configuration",
      target: ["identifier"],
      fields: ["identifier"],
      required: ["identifier"],
      result: {
        fields: ["action", "automation_id", "warnings"],
        objects: ["config", "verification"],
        rows: ["warnings", "best_practice_warnings"],
      },
    }),
  ],
  [
    "ha_config_set_automation",
    haAction({
      title: "Review automation configuration change",
      target: ["identifier"],
      fields: [
        "BestPracticeKey",
        "MandatoryBPS",
        "category",
        "config",
        "config_hash",
        "identifier",
        "python_transform",
        "wait",
      ],
      result: {
        fields: ["action", "automation_id", "entity_id", "warnings"],
        objects: ["config", "verification"],
        rows: ["warnings", "best_practice_warnings"],
      },
      modeTitle: (args) => (args.identifier ? "Update automation" : "Create automation"),
    }),
  ],
  [
    "ha_config_remove_automation",
    haAction({
      title: "Remove automation",
      target: ["identifier"],
      fields: ["identifier", "wait"],
      required: ["identifier"],
      result: { fields: ["action", "automation_id", "warnings"] },
    }),
  ],
  [
    "ha_config_get_scene",
    haAction({
      title: "Read scene configuration",
      target: ["scene_id"],
      fields: ["scene_id"],
      required: ["scene_id"],
      result: {
        fields: ["action", "scene_id", "warnings"],
        objects: ["config", "verification"],
        rows: ["warnings", "best_practice_warnings"],
      },
    }),
  ],
  [
    "ha_config_set_scene",
    haAction({
      title: "Save scene configuration",
      target: ["scene_id"],
      fields: [
        "BestPracticeKey",
        "MandatoryBPS",
        "category",
        "config",
        "config_hash",
        "python_transform",
        "scene_id",
        "wait",
      ],
      required: ["scene_id"],
      result: {
        fields: ["action", "scene_id", "entity_id", "warnings"],
        objects: ["config", "verification"],
        rows: ["warnings", "best_practice_warnings"],
      },
    }),
  ],
  [
    "ha_config_remove_scene",
    haAction({
      title: "Remove scene",
      target: ["scene_id"],
      fields: ["scene_id", "wait"],
      required: ["scene_id"],
      result: { fields: ["action", "scene_id", "warnings"] },
    }),
  ],
  [
    "ha_config_get_script",
    haAction({
      title: "Read script configuration",
      target: ["script_id"],
      fields: ["script_id"],
      required: ["script_id"],
      result: {
        fields: ["action", "script_id", "warnings"],
        objects: ["config", "verification"],
        rows: ["warnings", "best_practice_warnings"],
      },
    }),
  ],
  [
    "ha_config_set_script",
    haAction({
      title: "Save script configuration",
      target: ["script_id"],
      fields: [
        "BestPracticeKey",
        "MandatoryBPS",
        "category",
        "config",
        "config_hash",
        "python_transform",
        "script_id",
        "wait",
      ],
      required: ["script_id"],
      result: {
        fields: ["action", "script_id", "entity_id", "warnings"],
        objects: ["config", "verification"],
        rows: ["warnings", "best_practice_warnings"],
      },
    }),
  ],
  [
    "ha_config_remove_script",
    haAction({
      title: "Remove script",
      target: ["script_id"],
      fields: ["script_id", "wait"],
      required: ["script_id"],
      result: { fields: ["action", "script_id", "warnings"] },
    }),
  ],
  [
    "ha_config_get_dashboard",
    haAction({
      title: "Read Home Assistant dashboard",
      target: ["url_path", "view_path"],
      fields: [
        "card_type",
        "entity_id",
        "force_reload",
        "heading",
        "include_config",
        "include_screenshot",
        "list_only",
        "mode",
        "query",
        "url_path",
        "view_path",
      ],
      result: {
        fields: ["action", "url_path", "warnings", "match_count", "title"],
        rows: ["dashboards", "matches", "cards", "views"],
        objects: ["config"],
      },
      modeTitle: (args) =>
        args.list_only === true
          ? "List dashboards"
          : args.mode === "search" || args.entity_id || args.card_type || args.heading
            ? "Find dashboard cards"
            : "Read dashboard",
    }),
  ],
  [
    "ha_config_set_dashboard",
    haAction({
      title: "Save dashboard configuration",
      target: ["title", "url_path", "view_path"],
      fields: [
        "BestPracticeKey",
        "MandatoryBPS",
        "config",
        "config_hash",
        "icon",
        "python_transform",
        "require_admin",
        "return_screenshot",
        "show_in_sidebar",
        "title",
        "url_path",
        "view_path",
      ],
      required: ["url_path"],
      result: {
        fields: [
          "action",
          "url_path",
          "dashboard_created",
          "config_updated",
          "metadata_updated",
          "write_committed",
          "post_write_verified",
          "warnings",
        ],
      },
    }),
  ],
  [
    "ha_config_delete_dashboard",
    haAction({
      title: "Delete Home Assistant dashboard",
      target: ["url_path"],
      fields: ["url_path"],
      required: ["url_path"],
      result: { fields: ["action", "url_path"] },
    }),
  ],
  [
    "ha_config_list_dashboard_resources",
    haAction({
      title: "List dashboard resources",
      fields: ["include_content", "limit", "offset"],
      result: { rows: ["resources"], fields: ["count", "warnings"] },
    }),
  ],
  [
    "ha_config_set_dashboard_resource",
    haAction({
      title: "Save dashboard resource",
      target: ["resource_id", "url"],
      fields: ["content", "resource_id", "resource_type", "url"],
      result: { fields: ["action", "url", "resource_type", "resource_id", "warnings"] },
    }),
  ],
  [
    "ha_config_delete_dashboard_resource",
    haAction({
      title: "Delete dashboard resource",
      target: ["resource_id"],
      fields: ["resource_id"],
      required: ["resource_id"],
      result: { fields: ["action", "resource_id"] },
    }),
  ],
  [
    "ha_config_get_category",
    haAction({
      title: "Read Home Assistant categories",
      target: ["scope", "category_id"],
      fields: ["category_id", "scope"],
      required: ["scope"],
      result: { rows: ["categories"], objects: ["category"], fields: ["scope", "count"] },
      modeTitle: (args) => (args.category_id ? "Read category" : "List categories"),
    }),
  ],
  [
    "ha_config_set_category",
    haAction({
      title: "Save Home Assistant category",
      target: ["name", "scope"],
      fields: ["category_id", "icon", "name", "scope"],
      required: ["name", "scope"],
      result: { fields: ["scope"], objects: ["category_data"] },
      modeTitle: (args) => (args.category_id ? "Update category" : "Create category"),
    }),
  ],
  [
    "ha_config_remove_category",
    haAction({
      title: "Remove Home Assistant category",
      target: ["scope", "category_id"],
      fields: ["category_id", "scope"],
      required: ["scope", "category_id"],
      result: { fields: ["scope", "category_id"] },
    }),
  ],
  [
    "ha_config_get_label",
    haAction({
      title: "Read Home Assistant labels",
      target: ["label_id"],
      fields: ["label_id"],
      result: { rows: ["labels"], objects: ["label"], fields: ["count"] },
      modeTitle: (args) => (args.label_id ? "Read label" : "List labels"),
    }),
  ],
  [
    "ha_config_set_label",
    haAction({
      title: "Save Home Assistant label",
      target: ["name"],
      fields: ["color", "description", "icon", "label_id", "name"],
      required: ["name"],
      result: { objects: ["label_data"], fields: ["label_id"] },
      modeTitle: (args) => (args.label_id ? "Update label" : "Create label"),
    }),
  ],
  [
    "ha_config_remove_label",
    haAction({
      title: "Remove Home Assistant label",
      target: ["label_id"],
      fields: ["label_id"],
      required: ["label_id"],
      result: { fields: ["label_id"] },
    }),
  ],
  [
    "ha_config_list_groups",
    haAction({
      title: "List Home Assistant groups",
      fields: ["limit", "offset"],
      result: { rows: ["groups"], fields: ["count"] },
    }),
  ],
  [
    "ha_config_set_group",
    haAction({
      title: "Save Home Assistant group",
      target: ["name", "object_id"],
      fields: ["add_entities", "all_on", "entities", "icon", "name", "object_id", "remove_entities", "wait"],
      required: ["object_id"],
      result: { fields: ["entity_id", "updated_fields"] },
    }),
  ],
  [
    "ha_config_remove_group",
    haAction({
      title: "Remove Home Assistant group",
      target: ["object_id"],
      fields: ["object_id", "wait"],
      required: ["object_id"],
      result: { fields: ["entity_id", "object_id"] },
    }),
  ],
  [
    "ha_config_list_helpers",
    haAction({
      title: "List Home Assistant helpers",
      target: ["helper_type"],
      fields: ["helper_type", "limit", "offset"],
      required: ["helper_type"],
      result: { fields: ["helper_type", "count", "total_count"], rows: ["helpers"] },
    }),
  ],
  [
    "ha_config_set_helper",
    haAction({
      title: "Save Home Assistant helper",
      target: ["helper_type", "name", "helper_id"],
      fields: [
        "BestPracticeKey",
        "MandatoryBPS",
        "action",
        "area_id",
        "category",
        "config",
        "description",
        "device_trackers",
        "duration",
        "entry_id",
        "friday",
        "has_date",
        "has_time",
        "helper_id",
        "helper_type",
        "icon",
        "initial",
        "labels",
        "latitude",
        "longitude",
        "max_value",
        "min_value",
        "mode",
        "monday",
        "name",
        "options",
        "passive",
        "picture",
        "radius",
        "restore",
        "saturday",
        "show_advanced_options",
        "step",
        "subentry_id",
        "subentry_type",
        "sunday",
        "tag_id",
        "thursday",
        "tuesday",
        "unit_of_measurement",
        "user_id",
        "wait",
        "wednesday",
      ],
      required: ["helper_type"],
      result: {
        fields: ["action", "helper_type", "helper_id", "entity_id", "warnings", "step_id", "type"],
        rows: ["helpers", "entities"],
        objects: ["result", "config"],
      },
      modeTitle: (args) =>
        args.action === "create" ? "Create helper" : args.action === "update" ? "Update helper" : "Save helper",
    }),
  ],
  [
    "ha_remove_helpers_integrations",
    haAction({
      title: "Remove Home Assistant helper or integration",
      target: ["target", "helper_type"],
      fields: ["confirm", "helper_type", "subentry_id", "target", "wait"],
      required: ["target"],
      result: { fields: ["entity_id", "entry_id", "removed_count", "warnings"], rows: ["removed", "removal_results"] },
    }),
  ],
  [
    "ha_set_area_or_floor",
    haAction({
      title: "Save Home Assistant area or floor",
      target: ["name", "id"],
      fields: ["aliases", "floor_id", "icon", "id", "kind", "level", "name", "picture"],
      required: ["kind"],
      result: { fields: ["kind", "area_id", "floor_id"], objects: ["area", "floor"] },
      modeTitle: (args) =>
        args.kind === "floor" ? (args.id ? "Update floor" : "Create floor") : args.id ? "Update area" : "Create area",
    }),
  ],
  [
    "ha_remove_area_or_floor",
    haAction({
      title: "Remove Home Assistant area or floor",
      target: ["id"],
      fields: ["id", "kind"],
      required: ["kind", "id"],
      result: { fields: ["kind", "area_id", "floor_id"] },
      modeTitle: (args) => (args.kind === "floor" ? "Remove floor" : "Remove area"),
    }),
  ],
  [
    "ha_get_zone",
    haAction({
      title: "Read Home Assistant zones",
      target: ["zone_id"],
      fields: ["zone_id"],
      result: { rows: ["zones"], objects: ["zone"], fields: ["count"] },
      modeTitle: (args) => (args.zone_id ? "Read zone" : "List zones"),
    }),
  ],
  [
    "ha_set_zone",
    haAction({
      title: "Save Home Assistant zone",
      target: ["name", "zone_id"],
      fields: ["icon", "latitude", "longitude", "name", "passive", "radius", "zone_id"],
      result: { fields: ["updated_fields", "zone_id"], objects: ["zone_data"] },
      modeTitle: (args) => (args.zone_id ? "Update zone" : "Create zone"),
    }),
  ],
  [
    "ha_remove_zone",
    haAction({
      title: "Remove Home Assistant zone",
      target: ["zone_id"],
      fields: ["zone_id"],
      required: ["zone_id"],
      result: { fields: ["zone_id"] },
    }),
  ],
  [
    "ha_set_device",
    haAction({
      title: "Update Home Assistant device",
      target: ["device_id", "name"],
      fields: ["area_id", "device_id", "disabled_by", "labels", "name"],
      required: ["device_id"],
      result: { fields: ["name", "device_id", "area_id", "disabled_by", "updated_fields"], objects: ["device"] },
    }),
  ],
  [
    "ha_remove_device",
    haAction({
      title: "Remove Home Assistant device",
      target: ["device_id"],
      fields: ["device_id"],
      required: ["device_id"],
      result: { fields: ["device_name", "partial", "config_entries_removed"], rows: ["removal_results"] },
    }),
  ],
  [
    "ha_set_entity",
    haAction({
      title: "Update Home Assistant entity",
      target: ["entity_id"],
      fields: [
        "aliases",
        "area_id",
        "categories",
        "device_class",
        "enabled",
        "entity_id",
        "expose_to",
        "hidden",
        "icon",
        "label_operation",
        "labels",
        "name",
        "new_device_name",
        "new_entity_id",
        "options",
      ],
      required: ["entity_id"],
      result: {
        fields: ["entity_id", "name", "new_entity_id", "updated_fields", "warnings"],
        objects: ["entity", "changes"],
      },
    }),
  ],
  [
    "ha_remove_entity",
    haAction({
      title: "Remove Home Assistant entity",
      target: ["entity_id"],
      fields: ["entity_id"],
      required: ["entity_id"],
      result: { fields: ["entity_id"], outcome: "Entity removed" },
    }),
  ],
  [
    "ha_set_integration",
    haAction({
      title: "Review Home Assistant integration change",
      target: ["entry_id", "domain"],
      fields: [
        "config",
        "confirm_token",
        "domain",
        "enabled",
        "entry_id",
        "expected_device_id",
        "expected_entity_ids",
        "expected_mac",
        "expected_unique_id",
        "reconfigure",
      ],
      result: {
        fields: ["entry_id", "domain", "note", "status", "require_restart", "step_id", "type", "warnings"],
        objects: ["result", "flow", "entry"],
        reportsPhase: true,
      },
      modeTitle: (args) =>
        args.reconfigure === true
          ? "Reconfigure integration"
          : args.enabled === true
            ? "Enable integration"
            : args.enabled === false
              ? "Disable integration"
              : "Review integration setup",
    }),
  ],
]);
