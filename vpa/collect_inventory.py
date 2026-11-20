#!/usr/bin/env python3
"""Collect a sanitized, point-in-time Kubernetes VPA inventory.

Requires kubectl access to workload, Pod, VPA, metrics and Mimir query APIs.
Run: python vpa/collect_inventory.py > vpa/inventory.json
Raw Kubernetes objects and Prometheus labels other than namespace/pod/container
are not emitted. Reads are sequential, so this is a collection window, not an
atomic snapshot. Never collect or publish Secret objects, env vars or logs.
"""
import datetime as dt
import json
import subprocess
import urllib.parse
from collections import Counter, defaultdict


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get(*args):
    return json.loads(subprocess.check_output(["kubectl", *args], text=True))


def objs(kinds):
    return get("get", kinds, "-A", "-o", "json")["items"]


def resource(c):
    r = c.get("resources", {})
    return {x: {k: r.get(x, {}).get(k) for k in ("cpu", "memory")} for x in ("requests", "limits")}


def metadata(o):
    m = o["metadata"]
    return (m.get("namespace", ""), m["name"])


def group(version):
    return version.split("/")[0] if "/" in version else ""


def ref(o):
    return next((r for r in o.get("metadata", {}).get("ownerReferences", []) if r.get("controller")), None)


def key(o):
    ns, name = metadata(o)
    return (group(o.get("apiVersion", "")), o["kind"], ns, name)


def key_ref(r, ns):
    return (group(r.get("apiVersion", "")), r["kind"], ns, r["name"])


def quantity(value):
    if value is None:
        return None
    s = str(value)
    for suffix, scale in (("Ei", 1024**6), ("Pi", 1024**5), ("Ti", 1024**4), ("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024), ("E", 1000**6), ("P", 1000**5), ("T", 1000**4), ("G", 1000**3), ("M", 1000**2), ("k", 1000), ("m", .001), ("u", .000001), ("n", .000000001)):
        if s.endswith(suffix):
            return float(s[:-len(suffix)]) * scale
    return float(s)


started = now()
controllers = objs("deployments,statefulsets,daemonsets,cronjobs,jobs")
replicasets = objs("replicasets")
pods = objs("pods")
vpas = objs("verticalpodautoscalers")
hpas = objs("horizontalpodautoscalers")
namespaces = objs("namespaces")
metrics = get("get", "--raw", "/apis/metrics.k8s.io/v1beta1/pods")["items"]
query = 'max by (namespace,pod,container) (max_over_time(container_memory_working_set_bytes{container!="",container!="POD",pod!=""}[7d]))'
path = "/api/v1/namespaces/monitoring/services/http:mimir-query-frontend:8080/proxy/prometheus/api/v1/query?" + urllib.parse.urlencode({"query": query})
history = get("get", "--raw", path)
if history.get("status") != "success":
    raise RuntimeError("Mimir history query failed")
finished = now()

# An intermediate owner is trusted only when its UID matches the owner reference.
objects_by_uid = {o["metadata"]["uid"]: o for o in controllers + replicasets}
base = [o for o in controllers if o["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "CronJob")]
known = {key(o): o for o in base}
active = [p for p in pods if p.get("status", {}).get("phase") in ("Running", "Pending") and not p["metadata"].get("deletionTimestamp")]
by_owner = defaultdict(list)
for p in active:
    ns, name = metadata(p)
    owner = ref(p)
    if owner is None:
        dest = ("", "Pod", ns, name)
    else:
        dest = key_ref(owner, ns)
        # Only follow an actual controller reference, checking UID, namespace,
        # kind and name. Do not attribute Pods using name prefixes or labels.
        if owner["kind"] in ("ReplicaSet", "Job"):
            intermediate = objects_by_uid.get(owner.get("uid"))
            if intermediate and key(intermediate) == dest:
                higher = ref(intermediate)
                if higher:
                    parent = objects_by_uid.get(higher.get("uid"))
                    parent_key = key_ref(higher, ns)
                    if parent and key(parent) == parent_key:
                        dest = parent_key
        # UID matching also guards replacement of a same-named controller.
        if dest in known and owner["kind"] not in ("ReplicaSet", "Job"):
            if known[dest]["metadata"].get("uid") != owner.get("uid"):
                dest = ("", "Pod", ns, name)
    by_owner[dest].append(p)

vmap = defaultdict(list)
for v in vpas:
    target = v["spec"]["targetRef"]
    ns = v["metadata"]["namespace"]
    target_key = (group(target.get("apiVersion", "")), target["kind"], ns, target["name"])
    policies = v.get("spec", {}).get("resourcePolicy", {}).get("containerPolicies", [])
    vmap[target_key].append({"name": v["metadata"]["name"], "mode": v["spec"].get("updatePolicy", {}).get("updateMode", "Auto"), "minReplicas": v["spec"].get("updatePolicy", {}).get("minReplicas"), "containers": [{k: c[k] for k in ("containerName", "controlledResources", "controlledValues", "mode", "minAllowed", "maxAllowed") if k in c} for c in policies], "recommendations": [{k: c[k] for k in ("containerName", "target", "lowerBound", "upperBound", "uncappedTarget") if k in c} for c in v.get("status", {}).get("recommendation", {}).get("containerRecommendations", [])]})

hmap = defaultdict(list)
for h in hpas:
    t = h["spec"]["scaleTargetRef"]
    hmap[(group(t.get("apiVersion", "")), t["kind"], h["metadata"]["namespace"], t["name"])].append({"name": h["metadata"]["name"]})
nsmap = {o["metadata"]["name"]: o["metadata"].get("labels", {}) for o in namespaces}
metricmap = {(o["metadata"]["namespace"], o["metadata"]["name"]): {c["name"]: c.get("usage", {}) for c in o.get("containers", [])} for o in metrics}
peaks = {(z["metric"].get("namespace"), z["metric"].get("pod"), z["metric"].get("container")): float(z["value"][1]) for z in history.get("data", {}).get("result", []) if all(k in z["metric"] for k in ("namespace", "pod", "container"))}

rows = []
for owner in sorted(set(known) | set(by_owner)):
    api_group, kind, ns, name = owner
    obj = known.get(owner)
    matched = by_owner.get(owner, [])
    template = None
    if obj:
        template = obj["spec"]["jobTemplate"]["spec"]["template"] if kind == "CronJob" else obj["spec"]["template"]
    template_containers = {c["name"]: c for c in (template or {}).get("spec", {}).get("containers", [])}
    names = set(template_containers)
    names.update(c["name"] for pod in matched for c in pod.get("spec", {}).get("containers", []))
    containers = []
    for cname in sorted(names):
        template_container = template_containers.get(cname)
        admitted = []
        samples = []
        peak_samples = []
        for pod in matched:
            podname = pod["metadata"]["name"]
            for c in pod.get("spec", {}).get("containers", []):
                if c["name"] != cname:
                    continue
                m = metricmap.get((ns, podname), {}).get(cname, {})
                usage = quantity(m.get("memory"))
                peak = peaks.get((ns, podname, cname))
                if usage is not None:
                    samples.append(usage)
                if peak is not None:
                    peak_samples.append(peak)
                admitted.append({"pod": podname, "phase": pod.get("status", {}).get("phase"), "node": pod.get("spec", {}).get("nodeName"), **resource(c)})
        containers.append({"name": cname, "template": resource(template_container) if template_container else None, "admitted": admitted, "currentMemoryMaxMiB": round(max(samples)/2**20, 1) if samples else None, "sampledPods": len(samples), "peak7dCurrentPodMiB": round(max(peak_samples)/2**20, 1) if peak_samples else None, "peakSampledPods": len(peak_samples)})
    spec = obj.get("spec", {}) if obj else {}
    status = obj.get("status", {}) if obj else {}
    desired = status.get("desiredNumberScheduled") if kind == "DaemonSet" else spec.get("replicas", 1) if kind in ("Deployment", "StatefulSet") else None
    ready = status.get("numberReady", 0) if kind == "DaemonSet" else status.get("readyReplicas", 0) if kind in ("Deployment", "StatefulSet") else None
    labels = nsmap.get(ns, {})
    rows.append({"namespace": ns, "kind": kind, "apiGroup": api_group, "name": name, "scope": "controller-template" if obj else "pod-owner", "desiredReplicas": desired, "readyReplicas": ready, "activePods": len(matched), "containers": containers, "vpas": vmap.get(owner, []), "hpas": hmap.get(owner, []), "namespaceMode": labels.get("goldilocks.fairwinds.com/vpa-update-mode"), "namespaceEnabled": labels.get("goldilocks.fairwinds.com/enabled")})

active_controllers = [r for r in rows if r["scope"] == "controller-template" and r["kind"] in ("Deployment", "StatefulSet", "DaemonSet") and (r["desiredReplicas"] or 0) > 0]
admitted = [c for row in rows for z in row["containers"] for c in z["admitted"]]
assert len(admitted) == sum(len(p.get("spec", {}).get("containers", [])) for p in active)
counts = {"active_controllers": len(active_controllers), "active_direct_vpa": sum(bool(r["vpas"]) for r in active_controllers), "all_vpa_objects": len(vpas), "vpa_modes": dict(Counter(v["spec"].get("updatePolicy", {}).get("updateMode", "Auto") for v in vpas)), "active_pods": len(active), "regular_containers": len(admitted), "pod_owner_rows": sum(r["scope"] == "pod-owner" for r in rows)}
for typ in ("requests", "limits"):
    for unit in ("cpu", "memory"):
        counts[f"missing_{typ}_{unit}"] = sum(quantity(c[typ][unit]) in (None, 0) for c in admitted)
print(json.dumps({"collection_started_utc": started, "collection_finished_utc": finished, "history_end_utc": finished, "history_query": query, "counts": counts, "workloads": rows}, indent=2, ensure_ascii=False))
