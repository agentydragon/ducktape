#!/usr/bin/env bash
set -u
export GIT_PAGER=cat
export PAGER=cat
python3 - <<'PY'
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

SOURCE = "91f72591a1eb45b848a707277623d6b491d4b537"
BRANCH = "agent/test-architecture-pivot"
MCP = "agentplane/mcp_edge/internal/mcpserver/results.go"
RENDER = "agentplane/system_tests/deployment/render_fixture.py"
DEFS = "agentplane/system_tests/k3s/defs.bzl"
SUITES = "agentplane/system_tests/suites/BUILD.bazel"
ARCH = [RENDER, DEFS, SUITES]
COMMENT = b"// CI_SELECTOR_COUNTERFACTUAL_91f72591a1eb; synthetic comment-only input\n"
OUT = Path("/tmp/agents/2026_11_12/agentplane-ci-replan/iteration5/selector")
run_dir = OUT / ("run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}")
(run_dir / "commands").mkdir(parents=True, exist_ok=False)
(run_dir / "raw").mkdir()
commands, errors = [], []


def fail(scope, message):
    errors.append({"scope": scope, "message": message})
    print(f"SELECTOR_ERROR scope={scope} message={message}", flush=True)


def run(label, argv, cwd, env=None, required=True):
    n = len(commands) + 1
    stem = f"{n:03d}-" + re.sub(r"[^A-Za-z0-9_.-]+", "-", label)
    out, err = f"commands/{stem}.stdout", f"commands/{stem}.stderr"
    rec = {"label": label, "argv": argv, "cwd": str(cwd), "stdout": out, "stderr": err}
    try:
        with (run_dir / out).open("wb") as stdout, (run_dir / err).open("wb") as stderr:
            p = subprocess.run(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr, check=False)
        rec["returncode"] = p.returncode
        if p.returncode and required:
            fail(label, f"exit {p.returncode}; see {err}")
    except Exception as exc:
        rec["exception"] = repr(exc)
        if required:
            fail(label, repr(exc))
    commands.append(rec)
    with (run_dir / "commands.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def out_text(rec):
    p = run_dir / rec["stdout"]
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def git(cwd, *args, label, env=None, required=True):
    return run(label, ["git", *args], cwd, env=env, required=required)


def tree(cwd, index, staged, label):
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index)
    git(cwd, "read-tree", SOURCE, label=label + "-read", env=env)
    if staged:
        git(cwd, "add", "--", *staged, label=label + "-stage", env=env)
    rec = git(cwd, "write-tree", label=label + "-write", env=env)
    value = out_text(rec).strip()
    return value if rec.get("returncode") == 0 else None


def file_sha(cwd, path):
    return hashlib.sha256((cwd / path).read_bytes()).hexdigest()


def apply_control(wt):
    lines = (wt / RENDER).read_text(encoding="utf-8").splitlines(keepends=True)
    for variant in ("LOCAL_MODEL_POD", "LOCAL_MODEL_VM", "LOCAL_MODEL_FIRECRACKER"):
        start = next(i for i, line in enumerate(lines) if f"case FixtureVariant.{variant}:" in line)
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("        case FixtureVariant.") or lines[i].startswith("def main(")), len(lines))
        block = "".join(lines[start:end])
        old = "profile=fixture_chart.FixtureProfile.NO_MCP_EDGE"
        if block.count(old) != 1:
            raise ValueError(f"{variant}: expected one NO_MCP_EDGE profile")
        lines[start:end] = [block.replace(old, "profile=fixture_chart.FixtureProfile.FULL", 1)]
    (wt / RENDER).write_text("".join(lines), encoding="utf-8")
    defs = (wt / DEFS).read_text(encoding="utf-8")
    for stack in ("pod_scenarios", "vm", "firecracker"):
        old, new = f'"{stack}": struct(service_images = _COMMON_SERVICE_IMAGES,', f'"{stack}": struct(service_images = _SERVICE_IMAGES,'
        if defs.count(old) != 1:
            raise ValueError(f"{stack}: expected one COMMON_SERVICE_IMAGES stack")
        defs = defs.replace(old, new, 1)
    (wt / DEFS).write_text(defs, encoding="utf-8")
    return [RENDER, DEFS]


def arm(name, control, ci, root):
    print(f"SELECTOR_PHASE arm={name} start", flush=True)
    wt = OUT / (run_dir.name + "-worktrees") / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    add = git(root, "worktree", "add", "--detach", str(wt), SOURCE, label=name + "-worktree-add")
    result = {"name": name, "control_full_profiles": control, "status": "partial"}
    if add.get("returncode") != 0:
        return result
    try:
        overlay = apply_control(wt) if control else []
        idx = run_dir / (name + ".index")
        base_tree = tree(wt, idx, overlay, name + "-base-tree")
        source_tree = out_text(git(wt, "rev-parse", SOURCE + "^{tree}", label=name + "-source-tree")).strip()
        result.update({"base_tree": base_tree, "production_source_tree": source_tree, "control_overlay_paths": overlay})
        if not control and base_tree != source_tree:
            fail(name, "candidate base is not the production source tree")
        if control:
            diff = git(wt, "diff-tree", "--no-commit-id", "--name-only", "-r", source_tree, base_tree, label=name + "-overlay-diff")
            actual = sorted(x for x in out_text(diff).splitlines() if x)
            result["control_overlay_paths"] = actual
            if actual != sorted(overlay):
                fail(name, f"unexpected control overlay paths: {actual}")
        base_ids = {p: file_sha(wt, p) for p in ARCH + [MCP]}
        base_hash = run_dir / "raw" / f"{name}-base-hashes.json"
        base = run(name + "-base-hashes", ["bazel-diff", "generate-hashes", "-co=--proto:include_synthetic_attribute_hash", "-w", str(wt), "-b", "bazel", str(base_hash)], wt)
        (wt / MCP).write_bytes((wt / MCP).read_bytes() + (b"" if (wt / MCP).read_bytes().endswith(b"\n") else b"\n") + COMMENT)
        head_tree = tree(wt, idx, [*overlay, MCP], name + "-head-tree")
        path_diff = git(wt, "diff-tree", "--no-commit-id", "--name-only", "-r", base_tree or "", head_tree or "", label=name + "-synthetic-paths")
        changed = sorted(x for x in out_text(path_diff).splitlines() if x)
        if changed != [MCP]:
            fail(name, f"synthetic diff paths are {changed}")
        head_ids = {p: file_sha(wt, p) for p in ARCH + [MCP]}
        arch_stable = all(base_ids[p] == head_ids[p] for p in ARCH)
        if not arch_stable:
            fail(name, "architecture file identity changed within base/head pair")
        head_hash = run_dir / "raw" / f"{name}-head-hashes.json"
        head = run(name + "-head-hashes", ["bazel-diff", "generate-hashes", "-co=--proto:include_synthetic_attribute_hash", "-w", str(wt), "-b", "bazel", str(head_hash)], wt)
        result.update({"head_tree": head_tree, "synthetic_changed_paths": changed,
                       "file_sha256": {"base": base_ids, "head": head_ids},
                       "architecture_held_constant_within_pair": changed == [MCP] and arch_stable,
                       "hash_outputs": {"base": str(base_hash.relative_to(run_dir)), "head": str(head_hash.relative_to(run_dir))}})
        if base.get("returncode") != 0 or head.get("returncode") != 0 or not base_hash.exists() or not head_hash.exists():
            return result
        impact = run(name + "-get-impacted-targets", ["bazel-diff", "get-impacted-targets", "-w", str(wt), "-sh", str(base_hash), "-fh", str(head_hash)], wt)
        raw = [x.strip() for x in out_text(impact).splitlines() if x.strip()]
        raw_path = run_dir / "raw" / f"{name}-affected-raw.txt"
        raw_path.write_text("\n".join(raw) + ("\n" if raw else ""), encoding="utf-8")
        result["affected_raw_labels"] = raw
        if impact.get("returncode") != 0:
            return result
        literals = ci._quote_labels(raw_path)
        aq = run_dir / "raw" / f"{name}-affected-query.txt"
        aq.write_text(f'set({literals}) except kind("source file", set({literals})) except attr("tags", "manual", set({literals}))\n', encoding="utf-8")
        selected = run(name + "-affected-query", ["bazel", "query", f"--query_file={aq}"], wt)
        affected = sorted(x.strip() for x in out_text(selected).splitlines() if x.strip())
        af = run_dir / "raw" / f"{name}-affected-labels.txt"
        af.write_text("\n".join(affected) + ("\n" if affected else ""), encoding="utf-8")
        tq = run_dir / "raw" / f"{name}-test-query.txt"
        tq.write_text(f"tests(set({ci._quote_labels(af)}))\n", encoding="utf-8")
        tests = run(name + "-tests-query", ["bazel", "query", f"--query_file={tq}"], wt)
        test_labels = sorted(x.strip() for x in out_text(tests).splitlines() if x.strip())
        (run_dir / "raw" / f"{name}-pre-runtime-tag-filter-tests.txt").write_text("\n".join(test_labels) + ("\n" if test_labels else ""), encoding="utf-8")
        result.update({"affected_filter_query": str(aq.relative_to(run_dir)), "affected_filtered_labels": affected,
                       "pre_runtime_tag_filter_test_labels": test_labels,
                       "manual_targets_removed_by_filter": sorted(set(raw) - set(affected)),
                       "status": "complete" if selected.get("returncode") == 0 and tests.get("returncode") == 0 else "partial"})
        return result
    except Exception as exc:
        fail(name, repr(exc))
        result["exception"] = repr(exc)
        return result
    finally:
        git(root, "worktree", "remove", "--force", str(wt), label=name + "-worktree-remove")


def sha256_path(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def tools(root):
    result = {}
    for name, argv in (("git", ["git", "--version"]), ("bazel", ["bazel", "--version"]),
                       ("bazel_diff", ["bazel-diff", "--version"]), ("bb", ["bb", "--version"]),
                       ("python", ["python3", "--version"])):
        path = shutil.which(argv[0])
        rec = run("tool-version-" + name, argv, root, required=False)
        result[name] = {"path": path, "version_output": out_text(rec).strip(), "version_exit": rec.get("returncode"),
                        "binary_sha256": sha256_path(Path(path)) if path and Path(path).is_file() else None}
    return result


root = Path.cwd()
head = out_text(git(root, "rev-parse", "HEAD", label="production-head")).strip()
status = out_text(git(root, "status", "--porcelain", "--untracked-files=no", label="production-status")).splitlines()
parents = out_text(git(root, "show", "-s", "--format=%P", SOURCE, label="production-parents")).strip().split()
summary = {"schema": "agentplane-selector-counterfactual.v1",
           "experiment": "synthetic MCP-edge-only footprint; not a historical PR or observed saving",
           "production_source_sha": head, "expected_source_sha": SOURCE, "parents": parents,
           "remote_branch_label": BRANCH, "synthetic_change": {"path": MCP, "comment": COMMENT.decode().rstrip(),
           "comment_sha256": hashlib.sha256(COMMENT).hexdigest()}, "tools": tools(root),
           "source_tracked_status": status, "arms": [], "errors": errors,
           "manual_candidate_target_definitions_path": SUITES}
if head != SOURCE:
    fail("source", f"checkout is {head}, expected {SOURCE}")
if status:
    fail("source", f"tracked source tree is dirty: {status}")
try:
    spec = importlib.util.spec_from_file_location("ci_selector", root / "devinfra/ci/bazel_ci.py")
    ci = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ci
    spec.loader.exec_module(ci)
    summary["graph_wide_reason_for_synthetic_path"] = ci.graph_wide_change_reason([MCP])
    if summary["graph_wide_reason_for_synthetic_path"] is not None:
        fail("source", "synthetic MCP file unexpectedly triggers graph-wide selection")
    summary["ci_selector_source"] = "devinfra/ci/bazel_ci.py at pinned production source"
    summary["arms"] = [arm("control_full_profiles", True, ci, root), arm("candidate_no_mcp_edge", False, ci, root)]
except Exception as exc:
    fail("driver", repr(exc))
    summary["driver_exception"] = repr(exc)
if len(summary["arms"]) == 2:
    c, p = summary["arms"]
    checks = {f"{MCP}:{side}": c.get("file_sha256", {}).get(side, {}).get(MCP) == p.get("file_sha256", {}).get(side, {}).get(MCP)
              for side in ("base", "head")}
    checks.update({f"{SUITES}:{side}": c.get("file_sha256", {}).get(side, {}).get(SUITES) == p.get("file_sha256", {}).get(side, {}).get(SUITES)
                   for side in ("base", "head")})
    summary["cross_arm_identity_checks"] = checks
    summary["same_production_source_and_synthetic_input"] = head == SOURCE and all(checks.values())
    if not summary["same_production_source_and_synthetic_input"]:
        fail("cross-arm", f"source/MCP/manual-target identities differ: {checks}")
    if all("pre_runtime_tag_filter_test_labels" in a for a in summary["arms"]):
        cset, pset = (set(a["pre_runtime_tag_filter_test_labels"]) for a in (c, p))
        summary["selector_delta"] = {"control_only": sorted(cset - pset), "candidate_only": sorted(pset - cset),
                                     "control_count": len(cset), "candidate_count": len(pset),
                                     "interpretation": "selected labels before runtime tag filtering, not executed tests or saved feedback time"}
summary["errors"] = errors
summary["status"] = "complete" if not errors and len(summary["arms"]) == 2 and all(a.get("status") == "complete" for a in summary["arms"]) else "partial"
(run_dir / "result.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
archive_path = OUT / (run_dir.name + ".tar.gz")
try:
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(run_dir, arcname=run_dir.name)
    archive_info = {"size_bytes": archive_path.stat().st_size, "sha256": sha256_path(archive_path)}
except Exception as exc:
    archive_info = {"error": repr(exc)}
    fail("archive", repr(exc))
upload = {"attempted": False}
bb = shutil.which("bb")
if bb and archive_path.exists():
    rec = run("upload-archive", [bb, "upload", str(archive_path)], root, required=False)
    output = out_text(rec) + "\n" + (run_dir / rec["stderr"]).read_text(encoding="utf-8", errors="replace")
    digest = re.search(r"(?:/compressed-blobs/[^\s]+|/blobs/[^\s]+|[a-f0-9]{64}/\d+)", output, re.I)
    upload = {"attempted": True, "returncode": rec.get("returncode"), "cas_digest": digest.group(0) if digest else None}
    if rec.get("returncode") != 0 or not digest:
        fail("upload", "archive upload failed or returned no CAS digest; see outer log marker")
else:
    fail("upload", "bb unavailable or archive absent")
marker = {"schema": summary["schema"], "status": "complete" if not errors else "partial",
          "production_source_sha": head, "archive": archive_info, "upload": upload,
          "affected_label_counts": {a["name"]: len(a.get("affected_filtered_labels", [])) for a in summary["arms"]},
          "pre_runtime_tag_filter_test_counts": {a["name"]: len(a.get("pre_runtime_tag_filter_test_labels", [])) for a in summary["arms"]},
          "delta_counts": ({"control_only": len(summary["selector_delta"]["control_only"]),
                            "candidate_only": len(summary["selector_delta"]["candidate_only"])} if summary.get("selector_delta") else None),
          "error_count": len(errors)}
print("SELECTOR_COUNTERFACTUAL_RESULT_JSON " + json.dumps(marker, sort_keys=True), flush=True)
raise SystemExit(0 if not errors and summary["status"] == "complete" else 1)
PY
