"""kubernetes_admin Action presentation examples."""

import json

import pytest
import pytest_bazel

from agentplane.app.frontend.actions.testing.action_presentation_cases import ActionExample, ActionPresentationCases

pytest_plugins = ("testing.frontend_visual.fixtures", "agentplane.app.frontend.testing.visual_fixtures")
pytestmark = pytest.mark.asyncio(loop_scope="session")

CONFIG_MAP = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "settings", "namespace": "apps"},
    "data": {"mode": "safe"},
}

POD_YAML = """apiVersion: v1
kind: Pod
metadata:
  name: web-0
  namespace: apps
status:
  phase: Running
  conditions:
  - type: Ready
    status: "False"
  containerStatuses:
  - name: main
    ready: true
    restartCount: 0
  - name: worker
    ready: false
    restartCount: 2
    state:
      waiting:
        reason: CrashLoopBackOff
"""

POD_TABLE = (
    "NAMESPACE   APIVERSION   KIND   NAME    READY   STATUS             RESTARTS   AGE   LABELS\n"
    "apps        v1           Pod    web-0   1/2     CrashLoopBackOff   2          4m    app=web\n"
)

CONFIG_MAP_YAML = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings, namespace: apps}\ndata: {mode: safe}\n"

EVENTS_YAML = """# The following events (YAML format) were found:
- InvolvedObject: {Kind: Pod, Name: web-0, apiVersion: v1}
  Namespace: apps
  Timestamp: 2026-10-12 03:01:00 +0000 UTC
  Type: Warning
  Reason: BackOff
  Message: Back-off restarting failed container
"""


class TestActionPresentations(ActionPresentationCases):
    examples = (
        ActionExample(
            "kubernetes_admin",
            "pods_list_in_namespace",
            {"namespace": "apps", "labelSelector": "app=web", "fieldSelector": "status.phase=Running"},
            POD_TABLE,
            "1/2 ready",
        ),
        ActionExample(
            "kubernetes_admin",
            "resources_create_or_update",
            {"resource": json.dumps(CONFIG_MAP)},
            "# The following resources (YAML) have been created or updated successfully\n"
            "- " + CONFIG_MAP_YAML.strip().replace("\n", "\n  ") + "\n",
            "Resources applied",
        ),
        ActionExample(
            "kubernetes_admin",
            "pods_delete",
            {"name": "web-0", "namespace": "apps"},
            "Pod deleted successfully",
            "Deletion accepted",
            compact_card_expands=False,
        ),
        ActionExample(
            "kubernetes_admin",
            "pods_exec",
            {"name": "web-0", "namespace": "apps", "container": "main", "command": ["printf", "%s", "hello world"]},
            "hello world",
            "hello world",
        ),
        ActionExample(
            "kubernetes_admin",
            "resources_get",
            {"apiVersion": "v1", "kind": "ConfigMap", "name": "settings", "namespace": "apps"},
            CONFIG_MAP,
            "ConfigMap apps/settings",
            mcp_text=CONFIG_MAP_YAML,
        ),
        ActionExample(
            "kubernetes_admin",
            "resources_list",
            {"apiVersion": "v1", "kind": "Pod", "namespace": "apps", "labelSelector": "app=web"},
            {
                "items": [
                    {"Name": "web-0", "Namespace": "apps", "Ready": "1/2", "Status": "CrashLoopBackOff", "Restarts": 2}
                ]
            },
            "CrashLoopBackOff",
            mcp_text=POD_TABLE,
        ),
        ActionExample(
            "kubernetes_admin",
            "resources_delete",
            {"apiVersion": "v1", "kind": "Pod", "name": "web-0", "namespace": "apps", "gracePeriodSeconds": 0},
            "Resource deleted successfully",
            "Deletion accepted",
        ),
        ActionExample(
            "kubernetes_admin",
            "pods_get",
            {"name": "web-0", "namespace": "apps"},
            POD_YAML,
            "Not ready",
            compact_card_expands=False,
        ),
        ActionExample(
            "kubernetes_admin",
            "pods_log",
            {"name": "web-0", "namespace": "apps", "container": "main", "previous": True, "tail": 100},
            "Starting web server\nListening on :8080\n",
            "Listening on :8080",
        ),
        ActionExample(
            "kubernetes_admin",
            "events_list",
            {"namespace": "apps", "fieldSelector": "type=Warning"},
            EVENTS_YAML,
            "BackOff",
        ),
        ActionExample("kubernetes_admin", "pods_list", {"labelSelector": "app=web"}, POD_TABLE, "1/2 ready"),
        ActionExample(
            "kubernetes_admin",
            "namespaces_list",
            {"fieldSelector": "status.phase=Active"},
            "APIVERSION   KIND        NAME       STATUS   AGE   LABELS\n"
            "v1           Namespace   app-team   Active   4m    kubernetes.io/metadata.name=app-team\n",
            "Active",
        ),
        ActionExample(
            "kubernetes_admin",
            "projects_list",
            {},
            "- apiVersion: project.openshift.io/v1\n  kind: Project\n  metadata: {name: app-team}\n"
            "  status: {phase: Active}\n",
            "Project app-team",
            compact_card_expands=False,
        ),
        ActionExample(
            "kubernetes_admin",
            "nodes_log",
            {"name": "worker-1", "query": "kubelet", "tailLines": 80},
            "2026-10-12T03:00:00Z Pod sync complete\n2026-10-12T03:00:01Z Node status reported\n",
            "Node status reported",
        ),
        ActionExample(
            "kubernetes_admin",
            "nodes_stats_summary",
            {"name": "worker-1"},
            json.dumps(
                {
                    "node": {
                        "nodeName": "worker-1",
                        "cpu": {"time": "2026-10-12T03:00:00Z", "usageNanoCores": 280_000_000},
                        "memory": {
                            "time": "2026-10-12T03:00:00Z",
                            "usageBytes": 536_870_912,
                            "workingSetBytes": 268_435_456,
                        },
                    }
                }
            ),
            "0.28 cores",
            compact_card_expands=False,
        ),
        ActionExample(
            "kubernetes_admin",
            "nodes_top",
            {"label_selector": "pool=workers"},
            "NAME       CPU(cores)   CPU(%)   MEMORY(bytes)   MEMORY(%)\n"
            "worker-1   280m         7%       512Mi           12%\n",
            "Memory 512Mi (12%)",
        ),
        ActionExample(
            "kubernetes_admin",
            "pods_top",
            {"namespace": "apps", "all_namespaces": False, "label_selector": "app=web"},
            "NAMESPACE   NAME    CPU(cores)   MEMORY(bytes)\napps        web-0   40m          192Mi\n",
            "Memory 192Mi",
        ),
        ActionExample(
            "kubernetes_admin",
            "configuration_view",
            {"minified": True},
            "apiVersion: v1\nkind: Config\ncurrent-context: staging\n"
            "contexts:\n- name: staging\n  context: {cluster: staging-cluster, namespace: apps}\n",
            "staging-cluster",
            compact_card_expands=False,
        ),
        ActionExample(
            "kubernetes_admin",
            "pods_run",
            {"name": "preview-web", "namespace": "apps", "image": "registry.example.test/web:v1", "port": 8080},
            "# The following resources (YAML) have been created or updated successfully\n"
            "- apiVersion: v1\n  kind: Pod\n  metadata: {name: preview-web, namespace: apps}\n"
            "  status: {phase: Pending}\n"
            "- apiVersion: v1\n  kind: Service\n  metadata: {name: preview-web, namespace: apps}\n",
            "Pod applied",
            compact_card_expands=False,
        ),
        ActionExample(
            "kubernetes_admin",
            "resources_scale",
            {"apiVersion": "apps/v1", "kind": "Deployment", "name": "web", "namespace": "apps", "scale": 0},
            "# Current resource scale (YAML) is below\n"
            "apiVersion: autoscaling/v1\nkind: Scale\nmetadata: {name: web, namespace: apps}\n"
            "spec: {replicas: 0}\nstatus: {replicas: 2}\n",
            "Observed replicas · 2",
            compact_card_expands=False,
        ),
    )


if __name__ == "__main__":
    pytest_bazel.main()
