"""Build the Action screenshot suites with their provider's production renderer map."""

load("@bazel_skylib//rules:copy_file.bzl", "copy_file")
load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("//agentplane/app/frontend:visual_tests.bzl", "agentplane_visual_bundle", "agentplane_visual_test")

# Group, renderer module, exported map, owning Bazel target, browser shards.
_GROUPS = [
    ("environments", "session_delegation", "environmentsPresentationGroup", "session_delegation", 1),
    ("github", "github/index", "githubPresentationGroup", "github", 3),
    ("gmail", "gmail", "gmailPresentationGroup", "gmail", 1),
    ("google_calendar", "google_calendar", "googleCalendarPresentationGroup", "google_calendar", 1),
    ("grocy_sf", "grocy", "grocyPresentationGroup", "grocy", 3),
    ("home_assistant", "home_assistant/group", "homeAssistantPresentationGroup", "home_assistant_group", 5),
    ("kubernetes_admin", "kubernetes_admin/index", "kubernetesAdminPresentationGroup", "kubernetes_admin", 2),
    ("orchestration_v1", "orchestration", "orchestrationPresentationGroup", "orchestration", 1),
    ("sandbox", "sandbox", "sandboxPresentationGroup", "sandbox", 1),
    ("sessions", "session_delegation", "sessionsPresentationGroup", "session_delegation", 1),
    ("ssh", "ssh/index", "sshPresentationGroup", "ssh", 1),
    ("tana", "tana", "tanaPresentationGroup", "tana", 1),
]
VISUAL_ACTION_GROUPS = [group for group, _, _, _, _ in _GROUPS]

def visual_group_bundles():
    """Create independent browser entry points with the same host components as the App."""
    for group, module, binding, target, _ in _GROUPS:
        entry = group + "_entry.js"
        write_file(
            name = group + "_entry",
            out = entry,
            content = [
                'import { installActionVisuals } from "./visual_harness";',
                'import { %s } from "../rendering/%s";' % (binding, module),
                "installActionVisuals(new Map([%s]));" % binding,
            ],
        )
        agentplane_visual_bundle(
            name = group + "_bundle",
            entry_point = entry,
            output_dir = group,
            deps = [":visual_harness", "//agentplane/app/frontend/actions/rendering:" + target],
        )
    for directory in VISUAL_ACTION_GROUPS + ["operation"]:
        copy_file(
            name = directory + "_index",
            src = "//agentplane/app/frontend:visual_index_html",
            out = directory + "/index.html",
        )

def visual_group_tests():
    """Run every registered provider's examples at both production form factors."""
    for group, _, _, _, shards in _GROUPS:
        package = "//agentplane/app/frontend/actions/testing:"
        agentplane_visual_test(
            name = "visual_" + group,
            src = "test_visual_" + group + ".py",
            bundle = package + group + "_bundle",
            index = package + group + "_index",
            shard_count = shards,
            test_deps = [package + "action_presentation_cases"],
            title = "Agentplane: %s action presentations" % group,
        )
