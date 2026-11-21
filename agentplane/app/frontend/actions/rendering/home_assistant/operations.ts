import type { ActionPresentation } from "../presentation";
import { haAction } from "./secondary_common";

export const homeAssistantOperationsRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  [
    "ha_eval_template",
    haAction({
      title: "Evaluate Home Assistant template",
      fields: ["report_errors", "template", "timeout"],
      required: ["template"],
      result: { fields: ["error"], text: ["result"] },
    }),
  ],
  [
    "ha_get_app",
    haAction({
      title: "Read Home Assistant apps",
      target: ["slug"],
      fields: ["include_stats", "query", "repository", "slug", "source"],
      result: {
        fields: ["name", "version", "state", "source"],
        rows: ["addons", "repositories"],
        objects: ["addon", "stats", "summary"],
      },
      modeTitle: (args) => (args.slug ? "Read Supervisor app" : "List Supervisor apps"),
    }),
  ],
  [
    "ha_get_blueprint",
    haAction({
      title: "Read Home Assistant blueprint",
      target: ["domain", "path"],
      fields: ["domain", "path"],
      result: {
        fields: ["name", "path", "domain"],
        rows: ["blueprints"],
        objects: ["blueprint", "config"],
        text: ["content"],
      },
      modeTitle: (args) => (args.path ? "Read blueprint" : "List blueprints"),
    }),
  ],
  [
    "ha_get_hacs_info",
    haAction({
      title: "Read HACS repository information",
      target: ["query", "repository_id"],
      fields: ["action", "category", "installed_only", "max_results", "offset", "query", "repository_id"],
      required: ["action"],
      result: {
        fields: ["name", "full_name", "installed", "installed_version", "available_version", "pending_update", "count"],
        rows: ["results", "releases"],
        text: ["readme"],
      },
      modeTitle: (args) => (args.action === "search" ? "Search HACS repositories" : "Read HACS repository"),
    }),
  ],
  [
    "ha_get_skill_guide",
    haAction({
      title: "Read Home Assistant guide",
      target: ["skill", "file"],
      fields: ["file", "skill"],
      result: { fields: ["degraded", "description", "how_to_use"], rows: ["skills", "files"], text: ["content"] },
      modeTitle: (args) =>
        args.file ? "Read guide reference" : args.skill ? "Read skill guide" : "List Home Assistant guides",
    }),
  ],
  [
    "ha_import_blueprint",
    haAction({
      title: "Import Home Assistant blueprint",
      target: ["url"],
      fields: ["overwrite", "url"],
      required: ["url"],
      result: { fields: ["url", "overrides_existing"], objects: ["imported_blueprint"] },
    }),
  ],
  [
    "ha_manage_app",
    haAction({
      title: "Manage Home Assistant Supervisor app",
      target: ["slug", "repository"],
      fields: [
        "action",
        "array_patch",
        "auto_update",
        "body",
        "boot",
        "debug",
        "limit",
        "message_limit",
        "message_offset",
        "method",
        "network",
        "offset",
        "options",
        "path",
        "port",
        "python_transform",
        "repository",
        "request_headers",
        "slug",
        "summarize",
        "wait_for_close",
        "watchdog",
        "websocket",
      ],
      result: {
        fields: ["action", "name", "slug", "status", "warnings", "version", "response_status", "status_code"],
        rows: ["messages", "warnings"],
        objects: ["data", "result", "addon", "response"],
        text: ["body", "response_body"],
        reportsPhase: true,
      },
      modeTitle: (args) =>
        args.action === "add_repository"
          ? "Register Supervisor app repository"
          : args.action === "remove_repository"
            ? "Remove Supervisor app repository"
            : args.action === "install"
              ? "Install Supervisor app"
              : args.action === "uninstall"
                ? "Uninstall Supervisor app"
                : args.action === "start"
                  ? "Start Supervisor app"
                  : args.action === "stop"
                    ? "Stop Supervisor app"
                    : args.action === "restart"
                      ? "Restart Supervisor app"
                      : args.action === "update"
                        ? "Update Supervisor app"
                        : args.action === "rebuild"
                          ? "Rebuild Supervisor app"
                          : args.path
                            ? "Call Supervisor app API"
                            : args.action
                              ? "Review Supervisor app operation"
                              : "Change Supervisor app configuration",
    }),
  ],
  [
    "ha_manage_backup",
    haAction({
      title: "Manage Home Assistant backup",
      target: ["scope", "backup_id", "backup_name", "entity_id"],
      fields: [
        "action",
        "backup_id",
        "backup_name",
        "confirm",
        "domain",
        "entity_id",
        "limit",
        "name",
        "older_than_days",
        "restore_database",
        "scope",
      ],
      required: ["scope", "action"],
      result: {
        fields: ["action", "status", "slug", "warnings"],
        rows: ["backups"],
        objects: ["data", "backup", "result"],
        reportsPhase: true,
      },
      modeTitle: (args) =>
        args.action === "restore"
          ? "Restore Home Assistant backup"
          : args.action === "delete"
            ? "Delete Home Assistant backup"
            : args.action === "create"
              ? "Create Home Assistant backup"
              : args.action === "list"
                ? "List Home Assistant backups"
                : args.action === "view"
                  ? "Read backup contents"
                  : args.action === "diff"
                    ? "Compare backup to current configuration"
                    : "Review backup operation",
    }),
  ],
  [
    "ha_manage_energy_prefs",
    haAction({
      title: "Manage Home Assistant energy preferences",
      target: ["name"],
      fields: [
        "config",
        "config_hash",
        "dry_run",
        "included_in_stat",
        "mode",
        "name",
        "source",
        "stat_consumption",
        "water",
      ],
      required: ["mode"],
      result: {
        fields: ["mode", "target_key", "new_count", "dry_run", "valid", "warnings", "validation_errors"],
        rows: ["warnings", "errors"],
        objects: ["config", "validation"],
      },
      modeTitle: (args) =>
        args.mode === "get"
          ? "Read energy preferences"
          : args.mode === "add_device"
            ? "Add device energy tracking"
            : args.mode === "remove_device"
              ? "Remove device energy tracking"
              : args.mode === "add_source"
                ? "Add an energy source"
                : args.dry_run === true
                  ? "Preview energy preferences replacement"
                  : "Replace energy preferences",
    }),
  ],
  [
    "ha_manage_hacs",
    haAction({
      title: "Manage HACS repository",
      target: ["repository", "repository_id"],
      fields: ["action", "category", "repository", "repository_id", "version"],
      required: ["action"],
      result: {
        fields: ["name", "full_name", "status", "repository_id", "version", "warnings"],
        objects: ["data", "result", "repository"],
        reportsPhase: true,
      },
      modeTitle: (args) =>
        args.action === "add_repository"
          ? "Register HACS repository"
          : args.action === "download"
            ? "Download or update HACS repository"
            : args.action === "remove"
              ? "Remove HACS repository"
              : "Refresh HACS release information",
    }),
  ],
  [
    "ha_manage_pipeline",
    haAction({
      title: "Manage Home Assistant voice pipeline",
      target: ["pipeline_id", "name"],
      fields: [
        "action",
        "agent_id",
        "base_pipeline_id",
        "conversation_engine",
        "conversation_id",
        "conversation_language",
        "language",
        "make_preferred",
        "name",
        "pipeline_id",
        "prefer_local_intents",
        "sentence",
        "stt_engine",
        "stt_language",
        "tts_engine",
        "tts_language",
        "tts_voice",
        "wake_word_entity",
        "wake_word_id",
      ],
      required: ["action"],
      result: {
        fields: ["operation", "preferred_pipeline_id", "response", "language", "warnings"],
        rows: ["pipelines"],
        objects: ["pipeline", "response", "data"],
      },
      modeTitle: (args) =>
        args.action === "list"
          ? "List voice pipelines"
          : args.action === "get"
            ? "Read voice pipeline"
            : args.action === "create"
              ? "Create voice pipeline"
              : args.action === "update"
                ? "Update voice pipeline"
                : args.action === "set_preferred"
                  ? "Set preferred voice pipeline"
                  : "Process a sentence with the voice pipeline",
    }),
  ],
  [
    "ha_manage_radio",
    haAction({
      title: "Manage Home Assistant radio",
      target: ["radio", "device_id", "entity_id"],
      fields: ["action", "confirm", "device_id", "entity_id", "params", "radio"],
      required: ["radio", "action"],
      result: {
        fields: ["action", "radio", "status", "warnings", "device_id", "long_running"],
        rows: ["supported_actions", "devices", "nodes"],
        objects: ["data", "result", "diagnostics"],
        reportsPhase: true,
      },
      modeTitle: (args) =>
        args.action === "remove_device"
          ? "Remove radio device"
          : args.action === "firmware_update"
            ? "Request radio firmware update"
            : args.action === "commission" || args.action === "add"
              ? "Commission radio device"
              : args.action === "diagnostics"
                ? "Read radio diagnostics"
                : args.action === "network_status"
                  ? "Read radio network status"
                  : "Review radio operation",
    }),
  ],
  [
    "ha_manage_theme",
    haAction({
      title: "Manage Home Assistant theme",
      target: ["theme_name"],
      fields: ["action", "expected_current", "force", "mode", "theme_name", "value"],
      required: ["action"],
      result: { objects: ["data"], fields: ["warnings"] },
      modeTitle: (args) =>
        args.action === "list"
          ? "List installed themes"
          : args.action === "get_engine_theme"
            ? "Read screenshot account theme"
            : args.action === "set_engine_theme"
              ? "Set screenshot account theme"
              : "Set default Home Assistant theme",
    }),
  ],
  [
    "ha_manage_updates",
    haAction({
      title: "Manage Home Assistant updates",
      target: ["entity_ids"],
      fields: ["action", "backup", "categories", "entity_ids", "include_release_notes", "include_skipped"],
      result: {
        fields: ["updates_available", "skipped_count", "status", "warnings", "message"],
        rows: ["updates", "results"],
        objects: ["update", "data"],
        reportsPhase: true,
      },
      modeTitle: (args) =>
        args.action === "install"
          ? "Install Home Assistant updates"
          : args.action === "skip"
            ? "Skip offered update versions"
            : args.action === "clear_skipped"
              ? "Re-offer skipped update versions"
              : args.action === "get"
                ? "Read update details"
                : args.action == null || args.action === "list"
                  ? "List Home Assistant updates"
                  : "Review update operation",
    }),
  ],
  [
    "ha_reload_core",
    haAction({
      title: "Reload Home Assistant components",
      target: ["target", "entry_id"],
      fields: ["entry_id", "target"],
      result: {
        fields: ["target", "service", "status", "warnings", "failed", "succeeded"],
        rows: ["results", "warnings"],
        reportsPhase: true,
      },
      modeTitle: (args) =>
        args.target === "all" ? "Reload all reloadable Home Assistant components" : "Reload Home Assistant component",
    }),
  ],
  [
    "ha_report_issue",
    haAction({
      title: "Prepare Home Assistant diagnostic report",
      fields: ["fields", "tool_call_count"],
      result: {
        fields: ["suggested_title", "log_count", "startup_log_count"],
        objects: ["diagnostic_info"],
        text: ["formatted_report"],
      },
    }),
  ],
  [
    "ha_restart",
    haAction({
      title: "Review Home Assistant restart",
      fields: ["confirm"],
      result: { fields: ["message", "warnings"], outcome: "Restart initiated" },
      modeTitle: (args) =>
        args.confirm === true ? "Restart Home Assistant" : "Review Home Assistant restart confirmation",
    }),
  ],
]);
