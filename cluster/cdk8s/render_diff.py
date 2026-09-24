"""Tree-wide render-identity check: what Flux would apply at two git revisions.

Starting from the bootstrap `flux-system` Kustomization in `gotk-sync.yaml`, each revision
is walked the way Flux reconciles it: every rendered Flux `Kustomization` is rendered in
turn from its source, until no new Kustomization appears. Renders are compared per
Kustomization, keyed by (kind, namespace, name); Kustomization objects are compared
tree-wide, so a node moving between parents reads as a move.

Reproduced Flux semantics:

- `GitRepository` artifacts honour `sparseCheckout` (listed directories only) and the
  source-controller ignore matcher: its default patterns, `.sourceignore` files and
  `spec.ignore`, later wins, a matched directory prunes its subtree (go-git gitignore
  semantics). Symlinks are dropped. The ducktape repository resolves to the revision
  under test; other repositories are fetched at a pinned `ref.commit`/`ref.tag`, and a
  branch-tracking foreign source leaves its Kustomization unrendered.
- `ArtifactGenerator` copy operations per the source-watcher spec: `@alias/dir/**` copies
  the tree under `dir/` into the destination; `@alias/dir/` copies `dir` as a
  subdirectory; a plain file copies to `to` (into it when `to` ends in `/`); `exclude`
  matches the path relative to the source root or the non-glob `from` prefix, a pattern
  without `/` the file name. Only the `Overwrite` strategy is supported.
- kustomize-controller: a missing `kustomization.yaml` is generated from the `.yaml`/`.yml`
  files under `spec.path` (a subdirectory holding one is added whole); `targetNamespace`,
  `patches`, `images`, `namePrefix` and `nameSuffix` are written into it; the build runs
  without load restrictions. `postBuild` substitution is not applied; SOPS ciphertext is
  compared as-is.

Exit status 1 means the revisions render differently.
"""

import argparse
import concurrent.futures
import difflib
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class _Loader(yaml.CSafeLoader):
    """Safe loading, plus YAML 1.1's `=` value key, which kustomize emits unquoted."""


def _construct_value(loader: _Loader, node: yaml.Node) -> str:
    assert isinstance(node, yaml.ScalarNode)
    return str(loader.construct_scalar(node))


_Loader.add_constructor("tag:yaml.org,2002:value", _construct_value)

_DUCKTAPE_URL = re.compile(r"github\.com[:/]agentydragon/ducktape(\.git)?/?$")
_GOTK_SYNC = "cluster/k8s/flux/flux-system/gotk-sync.yaml"
_ROOT = ("flux-system", "flux-system")
_KUSTOMIZATION_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
_FLUX_KUSTOMIZATION_GROUP = "kustomize.toolkit.fluxcd.io"
# fluxcd/pkg/sourceignore: VCS, extension, CI and extra default patterns.
_DEFAULT_IGNORE = [
    ".git/",
    ".gitignore",
    ".gitmodules",
    ".gitattributes",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.png",
    "*.wmv",
    "*.flv",
    "*.tar.gz",
    "*.zip",
    ".github/",
    ".circleci/",
    ".travis.yml",
    ".gitlab-ci.yml",
    "appveyor.yml",
    ".drone.yml",
    "cloudbuild.yaml",
    "codeship-services.yml",
    "codeship-steps.yml",
    "**/.goreleaser.yml",
    "**/.sops.yaml",
    "**/.flux.yaml",
]
_UNSUPPORTED_SPEC = ("components", "commonMetadata")

type Json = Any
type KsKey = tuple[str, str]  # (namespace, name)
type ObjKey = tuple[str, str, str]  # (kind, namespace, name)


@dataclass(frozen=True)
class Blob:
    repo: Path
    sha: str


type Files = Mapping[str, Blob]


class RenderError(Exception):
    """A Kustomization that Flux would fail to build; reported, not fatal."""


class NotReproducibleError(Exception):
    """A Kustomization whose source this tool cannot reproduce offline."""


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, check=True).stdout


def git_tree(repo: Path, rev: str) -> dict[str, Blob]:
    """Regular files of `rev` (symlinks and submodules dropped, as Flux artifacts do)."""
    files = {}
    for entry in _git(repo, "ls-tree", "-r", "-z", "--full-tree", rev).split(b"\0"):
        if not entry:
            continue
        meta, path = entry.decode().split("\t", 1)
        mode, kind, sha = meta.split()
        if kind == "blob" and mode in ("100644", "100755"):
            files[path] = Blob(repo, sha)
    return files


class BlobStore:
    """Content-addressed blob files, hardlinked into assembled sources."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def path(self, sha: str) -> Path:
        return self.root / sha

    def fetch(self, blobs: Iterable[Blob]) -> None:
        by_repo: dict[Path, set[str]] = {}
        for blob in blobs:
            if not self.path(blob.sha).exists():
                by_repo.setdefault(blob.repo, set()).add(blob.sha)
        for repo, shas in by_repo.items():
            out = _git(repo, "cat-file", "--batch", stdin="".join(f"{s}\n" for s in shas).encode())
            pos = 0
            while pos < len(out):
                header_end = out.index(b"\n", pos)
                sha, _, size = out[pos:header_end].decode().split()
                start = header_end + 1
                tmp = self.root / f".{sha}.tmp"
                tmp.write_bytes(out[start : start + int(size)])
                tmp.replace(self.path(sha))
                pos = start + int(size) + 1


@dataclass(frozen=True)
class IgnorePattern:
    """One gitignore line, matched as go-git's `plumbing/format/gitignore` does."""

    domain: tuple[str, ...]
    parts: tuple[str, ...]
    inclusion: bool
    dir_only: bool
    is_glob: bool

    @classmethod
    def parse(cls, line: str, domain: tuple[str, ...] = ()) -> IgnorePattern:
        inclusion = line.startswith("!")
        line = line.removeprefix("!")
        if not line.endswith("\\ "):
            line = line.rstrip(" ")
        dir_only = line.endswith("/")
        line = line.removesuffix("/")
        return cls(domain, tuple(line.split("/")), inclusion, dir_only, "/" in line)

    def match(self, path: tuple[str, ...], is_dir: bool) -> bool | None:
        """True: excluded, False: re-included, None: no opinion."""
        if len(path) <= len(self.domain) or path[: len(self.domain)] != self.domain:
            return None
        rest = path[len(self.domain) :]
        hit = self._glob_match(rest, is_dir) if self.is_glob else self._name_match(rest, is_dir)
        return None if not hit else not self.inclusion

    def _name_match(self, path: tuple[str, ...], is_dir: bool) -> bool:
        for i, name in enumerate(path):
            if fnmatch.fnmatchcase(name, self.parts[0]):
                return not (self.dir_only and not is_dir and i == len(path) - 1)
        return False

    def _glob_match(self, path: tuple[str, ...], is_dir: bool) -> bool:
        matched = can_traverse = False
        for i, part in enumerate(self.parts):
            if part == "":
                can_traverse = False
                continue
            if part == "**":
                if i == len(self.parts) - 1:
                    break
                can_traverse = True
                continue
            if "**" in part or not path:
                return False
            if can_traverse:
                can_traverse = False
                while path:
                    head, path = path[0], path[1:]
                    if fnmatch.fnmatchcase(head, part):
                        matched = True
                        break
                    if not path:
                        matched = False
            else:
                if not fnmatch.fnmatchcase(path[0], part):
                    return False
                matched = True
                path = path[1:]
        return not (matched and self.dir_only and not is_dir and not path) and matched


def _read_patterns(text: str, domain: tuple[str, ...] = ()) -> list[IgnorePattern]:
    return [
        IgnorePattern.parse(line, domain) for line in text.splitlines() if line.strip() and not line.startswith("#")
    ]


def flux_ignore(files: Mapping[str, Blob], ignore: str | None, store: BlobStore) -> dict[str, Blob]:
    """The files a source-controller artifact keeps."""
    sourceignores = sorted(p for p in files if posixpath.basename(p) == ".sourceignore")
    store.fetch(files[p] for p in sourceignores)
    patterns = [IgnorePattern.parse(p) for p in _DEFAULT_IGNORE]
    for p in sourceignores:
        domain = tuple(posixpath.dirname(p).split("/")) if "/" in p else ()
        patterns += _read_patterns(store.path(files[p].sha).read_text(), domain)
    patterns += _read_patterns(ignore or "")

    def excluded(parts: tuple[str, ...], is_dir: bool) -> bool:
        for pattern in reversed(patterns):
            if (verdict := pattern.match(parts, is_dir)) is not None:
                return verdict
        return False

    dir_excluded: dict[tuple[str, ...], bool] = {}

    def dir_is_excluded(parts: tuple[str, ...]) -> bool:
        if not parts:
            return False
        if parts not in dir_excluded:
            dir_excluded[parts] = dir_is_excluded(parts[:-1]) or excluded(parts, True)
        return dir_excluded[parts]

    return {
        path: blob
        for path, blob in files.items()
        if not dir_is_excluded(tuple(path.split("/")[:-1])) and not excluded(tuple(path.split("/")), False)
    }


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """doublestar glob: `**` spans directories, `*`/`?` do not, `{a,b}` alternates."""
    out, i, depth = "", 0, 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
            continue
        if pattern.startswith("**", i):
            out, i = out + ".*", i + 2
            continue
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[":
            end = pattern.index("]", i + 1)
            body = pattern[i + 1 : end]
            out += "[" + ("^" + body[1:] if body.startswith(("!", "^")) else body) + "]"
            i = end
        elif c == "{":
            out, depth = out + "(?:", depth + 1
        elif c == "}" and depth:
            out, depth = out + ")", depth - 1
        elif c == "," and depth:
            out += "|"
        else:
            out += re.escape(c)
        i += 1
    return re.compile(out + r"\Z")


def _has_glob(part: str) -> bool:
    return any(c in part for c in "*?[{")


def apply_copy(sources: Mapping[str, Files], op: Mapping[str, Json], artifact: dict[str, Blob]) -> None:
    """One ArtifactGenerator copy operation into `artifact` (Overwrite strategy)."""
    if op.get("strategy", "Overwrite") != "Overwrite":
        raise NotReproducibleError(f"copy strategy {op['strategy']!r} is not reproduced")
    alias, _, pattern = op["from"].removeprefix("@").partition("/")
    dest = op["to"].removeprefix("@artifact/")
    if not op["to"].startswith("@artifact/") or alias not in sources:
        raise NotReproducibleError(f"copy {op['from']} -> {op['to']}: unknown alias or destination")
    files = sources[alias]
    parts = pattern.split("/")
    split = next((i for i, part in enumerate(parts) if _has_glob(part)), None)
    if split is not None:
        base = "/".join(parts[:split])
        regex = _glob_regex(pattern)
        picked = {p: posixpath.relpath(p, base) if base else p for p in files if regex.match(p)}
        dest_dir = dest
    elif pattern.endswith("/") or (pattern not in files and any(p.startswith(pattern + "/") for p in files)):
        base = pattern.rstrip("/")
        picked = {
            p: posixpath.join(posixpath.basename(base), p[len(base) + 1 :]) for p in files if p.startswith(base + "/")
        }
        dest_dir = dest
    elif pattern in files:
        base = posixpath.dirname(pattern)
        picked = {pattern: posixpath.basename(pattern)}
        dest_dir = dest if dest.endswith("/") or not dest else None
    else:
        picked, base, dest_dir = {}, "", dest
    excludes = [_glob_regex(x) for x in op.get("exclude", [])]
    picked = {
        src: rel
        for src, rel in picked.items()
        if not any(
            rx.match(src) or rx.match(rel) or ("/" not in x and rx.match(posixpath.basename(src)))
            for x, rx in zip(op.get("exclude", []), excludes, strict=True)
        )
    }
    if not picked and not op.get("optional", False):
        raise RenderError(f"copy {op['from']} matched no files")
    for src, rel in picked.items():
        artifact[posixpath.join(dest_dir, rel) if dest_dir is not None else dest] = files[src]


@dataclass
class Revision:
    """What one revision's Flux graph is resolved from."""

    repo: Path
    sha: str
    tree: dict[str, Blob]
    upstream_cache: Path
    store: BlobStore
    git_repositories: dict[KsKey, Json] = field(default_factory=dict)
    generators: dict[KsKey, Json] = field(default_factory=dict)
    _git_sources: dict[str, Files] = field(default_factory=dict)

    def git_source(self, obj: Json) -> Files:
        spec = obj["spec"]
        cache_key = json.dumps(spec, sort_keys=True)
        if cache_key not in self._git_sources:
            tree = self.tree if _DUCKTAPE_URL.search(spec["url"]) else self._upstream(spec)
            if sparse := spec.get("sparseCheckout"):
                prefixes = tuple(d.strip("/") + "/" for d in sparse)
                tree = {p: b for p, b in tree.items() if p.startswith(prefixes)}
            self._git_sources[cache_key] = flux_ignore(tree, spec.get("ignore"), self.store)
        return self._git_sources[cache_key]

    def _upstream(self, spec: Json) -> dict[str, Blob]:
        ref = spec.get("ref", {})
        if "commit" in ref:
            want = ref["commit"]
            fetch = [want]
        elif "tag" in ref:
            want = f"refs/tags/{ref['tag']}"
            fetch = [f"{want}:{want}"]
        else:
            raise NotReproducibleError(f"{spec['url']} tracks a moving ref {ref}")
        repo = self.upstream_cache / hashlib.sha256(spec["url"].encode()).hexdigest()[:16]
        if not repo.exists():
            subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
        verify = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "-q", "--verify", f"{want}^{{commit}}"],
            capture_output=True,
            check=False,
        )
        if verify.returncode != 0:
            _git(repo, "fetch", "-q", "--depth=1", "--no-tags", spec["url"], *fetch)
        return git_tree(repo, f"{want}^{{commit}}")

    def artifact(self, namespace: str, name: str) -> Files:
        for gen in self.generators.values():
            if gen["metadata"]["namespace"] != namespace:
                continue
            for art in gen["spec"]["artifacts"]:
                if art["name"] != name:
                    continue
                if "pathPattern" in gen["spec"]:
                    raise NotReproducibleError("ArtifactGenerator pathPattern is not reproduced")
                sources = {}
                for src in gen["spec"]["sources"]:
                    if src["kind"] != "GitRepository":
                        raise NotReproducibleError(f"ArtifactGenerator source kind {src['kind']} is not reproduced")
                    repo_key = (src.get("namespace", namespace), src["name"])
                    if repo_key not in self.git_repositories:
                        raise KeyError(repo_key)
                    sources[src["alias"]] = self.git_source(self.git_repositories[repo_key])
                files: dict[str, Blob] = {}
                for op in art["copy"]:
                    apply_copy(sources, op, files)
                return files
        raise KeyError((namespace, name))

    def source_files(self, ks: Json) -> Files:
        """KeyError while the source is not (yet) known."""
        ref = ks["spec"]["sourceRef"]
        key = (ref.get("namespace", ks["metadata"]["namespace"]), ref["name"])
        match ref["kind"]:
            case "GitRepository":
                return self.git_source(self.git_repositories[key])
            case "ExternalArtifact":
                return self.artifact(*key)
            case kind:
                raise NotReproducibleError(f"sourceRef kind {kind} is not reproduced")


@dataclass(frozen=True)
class Rendered:
    objects: dict[ObjKey, Json]


@dataclass(frozen=True)
class Failed:
    error: str


@dataclass(frozen=True)
class NotRendered:
    reason: str


type Result = Rendered | Failed | NotRendered


def _overlay(ks_spec: Json) -> Json:
    for key in _UNSUPPORTED_SPEC:
        if key in ks_spec:
            raise NotReproducibleError(f"spec.{key} is not reproduced")
    return {k: ks_spec[k] for k in ("targetNamespace", "patches", "images", "namePrefix", "nameSuffix") if k in ks_spec}


def _write_kustomization(build_dir: Path, overlay: Json) -> None:
    """kustomize-controller's generator: find or generate the kustomization file, then patch it."""
    existing = next((build_dir / n for n in _KUSTOMIZATION_FILES if (build_dir / n).is_file()), None)
    if existing is None:
        resources = []
        for dirpath, dirnames, filenames in os.walk(build_dir):
            dirnames.sort()
            here = Path(dirpath)
            for d in list(dirnames):
                if any((here / d / n).is_file() for n in _KUSTOMIZATION_FILES):
                    resources.append((here / d).relative_to(build_dir).as_posix())
                    dirnames.remove(d)
            resources += [
                (here / f).relative_to(build_dir).as_posix() for f in sorted(filenames) if f.endswith((".yaml", ".yml"))
            ]
        doc: Json = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": resources}
        existing = build_dir / "kustomization.yaml"
    elif not overlay:
        return
    else:
        doc = yaml.load(existing.read_text(), Loader=_Loader)
        existing.unlink()  # a hardlink into the blob store: never write through it
    if "targetNamespace" in overlay:
        doc["namespace"] = overlay["targetNamespace"]
    for key in ("patches", "images"):
        doc[key] = doc.get(key, []) + overlay.get(key, [])
    for key in ("namePrefix", "nameSuffix"):
        if key in overlay:
            doc[key] = overlay[key]
    existing.write_text(yaml.safe_dump({k: v for k, v in doc.items() if v != []}))


def _object_key(obj: Json) -> ObjKey:
    meta = obj.get("metadata", {})
    return (obj["kind"], meta.get("namespace", ""), meta["name"])


def parse_objects(text: str) -> dict[ObjKey, Json]:
    objects: dict[ObjKey, Json] = {}
    for obj in yaml.load_all(text, Loader=_Loader):
        if obj is None:
            continue
        key = _object_key(obj)
        if key in objects:
            raise RenderError(f"duplicate object {key}")
        objects[key] = obj
    return objects


class Renderer:
    """Builds Kustomizations, caching output by the digest of everything the build reads."""

    def __init__(self, cache: Path, store: BlobStore, kustomize: str) -> None:
        self.cache = cache / "renders"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.work = cache / "work"
        self.work.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.kustomize = kustomize
        self.version = subprocess.run([kustomize, "version"], capture_output=True, text=True, check=True).stdout
        self.hits = self.builds = 0

    def key(self, files: Files, path: str, overlay: Json) -> str:
        h = hashlib.sha256(json.dumps([self.version, path, overlay], sort_keys=True).encode())
        for p in sorted(files):
            h.update(f"{p}\0{files[p].sha}\n".encode())
        return h.hexdigest()

    def cached(self, key: str) -> Result | None:
        out, err = self.cache / f"{key}.yaml", self.cache / f"{key}.err"
        if out.exists():
            return Rendered(parse_objects(out.read_text()))
        if err.exists():
            return Failed(err.read_text())
        return None

    def build(self, key: str, files: Files, path: str, overlay: Json) -> Result:
        with tempfile.TemporaryDirectory(dir=self.work) as tmp:
            root = Path(tmp)
            for rel, blob in files.items():
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.link(self.store.path(blob.sha), dest)
            build_dir = root / path
            if not build_dir.is_dir():
                error = f"path {path!r} not found in source"
            else:
                _write_kustomization(build_dir, overlay)
                proc = subprocess.run(
                    [self.kustomize, "build", "--load-restrictor", "LoadRestrictionsNone", str(build_dir)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                error = proc.stderr.replace(tmp, "<src>").strip() if proc.returncode else ""
        if error:
            (self.cache / f"{key}.err").write_text(error)
            return Failed(error)
        (self.cache / f"{key}.yaml").write_text(proc.stdout)
        return Rendered(parse_objects(proc.stdout))


@dataclass
class Graph:
    """One revision's reconciled Flux graph."""

    kustomizations: dict[KsKey, Json] = field(default_factory=dict)
    parents: dict[KsKey, KsKey | None] = field(default_factory=dict)
    results: dict[KsKey, Result] = field(default_factory=dict)


def _is_flux_kustomization(obj: Json) -> bool:
    return bool(obj["kind"] == "Kustomization") and str(obj["apiVersion"]).startswith(_FLUX_KUSTOMIZATION_GROUP + "/")


def reconcile(rev: Revision, renderer: Renderer, jobs: int) -> Graph:
    graph = Graph()
    rev.store.fetch([rev.tree[_GOTK_SYNC]])
    for obj in yaml.load_all(rev.store.path(rev.tree[_GOTK_SYNC].sha).read_text(), Loader=_Loader):
        seed = (obj["metadata"]["namespace"], obj["metadata"]["name"])
        if obj["kind"] == "GitRepository":
            rev.git_repositories[seed] = obj
        elif _is_flux_kustomization(obj):
            graph.kustomizations[seed], graph.parents[seed] = obj, None
    pending = {_ROOT}
    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        while pending:
            ready: dict[KsKey, tuple[str, Files, str, Json]] = {}
            for ks_key in sorted(pending):
                ks = graph.kustomizations[ks_key]
                try:
                    files = rev.source_files(ks)
                    overlay = _overlay(ks["spec"])
                except KeyError:
                    continue
                except (NotReproducibleError, RenderError) as e:
                    graph.results[ks_key] = (NotRendered if isinstance(e, NotReproducibleError) else Failed)(str(e))
                    continue
                path = posixpath.normpath(ks["spec"]["path"].lstrip("/"))
                ready[ks_key] = (renderer.key(files, path, overlay), files, path, overlay)
            pending -= set(graph.results)
            if not ready:
                break
            misses = {}
            for ks_key, (key, files, path, overlay) in ready.items():
                if (hit := renderer.cached(key)) is not None:
                    graph.results[ks_key] = hit
                    renderer.hits += 1
                else:
                    misses[ks_key] = (key, files, path, overlay)
            rev.store.fetch(blob for (_, files, _, _) in misses.values() for blob in files.values())
            futures = {k: pool.submit(renderer.build, *args) for k, args in misses.items()}
            for ks_key, future in futures.items():
                graph.results[ks_key] = future.result()
                renderer.builds += 1
            pending -= set(ready)
            for ks_key in ready:
                result = graph.results[ks_key]
                if not isinstance(result, Rendered):
                    continue
                for obj in result.objects.values():
                    ns_key = (obj["metadata"].get("namespace", ""), obj["metadata"]["name"])
                    if _is_flux_kustomization(obj) and ns_key not in graph.kustomizations:
                        graph.kustomizations[ns_key], graph.parents[ns_key] = obj, ks_key
                        pending.add(ns_key)
                    elif obj["kind"] == "GitRepository":
                        rev.git_repositories.setdefault(ns_key, obj)
                    elif obj["kind"] == "ArtifactGenerator":
                        rev.generators.setdefault(ns_key, obj)
    for ks_key in pending:
        spec = graph.kustomizations[ks_key]["spec"]
        ref = spec["sourceRef"]
        graph.results[ks_key] = NotRendered(
            f"{'suspended; ' if spec.get('suspend') else ''}{ref['kind']} {ref['name']} is not in the reconciled graph"
        )
    return graph


def _fmt(value: Json, limit: int = 100) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _named(items: list[Json]) -> dict[str, Json] | None:
    if not all(isinstance(i, dict) and isinstance(i.get("name"), str) for i in items):
        return None
    named = {i["name"]: i for i in items}
    return named if len(named) == len(items) else None


def field_diff(old: Json, new: Json, path: str = "") -> Iterator[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(old.keys() | new.keys(), key=str):
            sub = f"{path}.{k}" if path else str(k)
            if k not in new:
                yield f"{sub}: removed {_fmt(old[k])}"
            elif k not in old:
                yield f"{sub}: added {_fmt(new[k])}"
            else:
                yield from field_diff(old[k], new[k], sub)
    elif isinstance(old, list) and isinstance(new, list):
        old_named, new_named = _named(old), _named(new)
        if old_named is not None and new_named is not None:
            if list(old_named) != list(new_named) and old_named.keys() == new_named.keys():
                yield f"{path}: reordered"
            for name in old_named.keys() | new_named.keys():
                sub = f"{path}[name={name}]"
                if name not in new_named:
                    yield f"{sub}: removed"
                elif name not in old_named:
                    yield f"{sub}: added {_fmt(new_named[name])}"
                else:
                    yield from field_diff(old_named[name], new_named[name], sub)
        else:
            for i in range(max(len(old), len(new))):
                if i >= len(new):
                    yield f"{path}[{i}]: removed {_fmt(old[i])}"
                elif i >= len(old):
                    yield f"{path}[{i}]: added {_fmt(new[i])}"
                else:
                    yield from field_diff(old[i], new[i], f"{path}[{i}]")
    elif old != new:
        if isinstance(old, str) and isinstance(new, str) and "\n" in old + new:
            lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0))[2:]
            shown = [ln for ln in lines if not ln.startswith("@@")][:12]
            yield f"{path}: text changed" + "".join(f"\n    {ln[:120]}" for ln in shown)
        else:
            yield f"{path}: {_fmt(old)} -> {_fmt(new)}"


def _obj_label(key: ObjKey) -> str:
    kind, ns, name = key
    return f"{kind} {ns}/{name}" if ns else f"{kind} {name}"


def _ks_label(key: KsKey | None) -> str:
    return "(bootstrap)" if key is None else f"{key[0]}/{key[1]}"


def _strip_flux_kustomizations(result: Result) -> Result:
    if not isinstance(result, Rendered):
        return result
    return Rendered({k: v for k, v in result.objects.items() if not _is_flux_kustomization(v)})


def compare(base: Graph, head: Graph) -> list[str]:
    """Report lines; empty when the revisions reconcile identically."""
    lines: list[str] = []
    ks_lines = []
    for key in sorted(base.kustomizations.keys() | head.kustomizations.keys()):
        if key not in head.kustomizations:
            ks_lines.append(f"- {_ks_label(key)} (was under {_ks_label(base.parents[key])})")
        elif key not in base.kustomizations:
            ks_lines.append(f"+ {_ks_label(key)} (under {_ks_label(head.parents[key])})")
        else:
            changes = list(field_diff(base.kustomizations[key], head.kustomizations[key]))
            if base.parents[key] != head.parents[key]:
                changes.insert(0, f"parent: {_ks_label(base.parents[key])} -> {_ks_label(head.parents[key])}")
            if changes:
                ks_lines.append(f"~ {_ks_label(key)}" + "".join(f"\n    {c}" for c in changes))
    if ks_lines:
        lines += ["== Flux Kustomization objects ==", *ks_lines]

    removed: dict[ObjKey, tuple[KsKey, Json]] = {}
    added: dict[ObjKey, tuple[KsKey, Json]] = {}
    per_ks: dict[KsKey, list[tuple[str, ObjKey, list[str]]]] = {}
    for ks in sorted(base.results.keys() | head.results.keys()):
        old = _strip_flux_kustomizations(base.results.get(ks, Rendered({})))
        new = _strip_flux_kustomizations(head.results.get(ks, Rendered({})))
        entries = per_ks.setdefault(ks, [])
        if not (isinstance(old, Rendered) and isinstance(new, Rendered)):
            if old != new:
                entries.append(("!", ("", "", ""), [f"base: {_describe(old)}", f"head: {_describe(new)}"]))
            continue
        for obj in sorted(old.objects.keys() | new.objects.keys()):
            if obj not in new.objects:
                removed[obj] = (ks, old.objects[obj])
                entries.append(("-", obj, []))
            elif obj not in old.objects:
                added[obj] = (ks, new.objects[obj])
                entries.append(("+", obj, []))
            elif changes := list(field_diff(old.objects[obj], new.objects[obj])):
                entries.append(("~", obj, changes))
    for ks, entries in per_ks.items():
        if not entries:
            continue
        lines.append(f"== {_ks_label(ks)} ==")
        for sign, obj, changes in entries:
            if sign == "!":
                lines += [f"! {c}" for c in changes]
                continue
            note = ""
            if sign == "-" and obj in added and added[obj][0] != ks:
                other_ks, other = added[obj]
                note = f" (moved to {_ks_label(other_ks)}{'' if other == removed[obj][1] else ', changed'})"
            elif sign == "+" and obj in removed and removed[obj][0] != ks:
                note = f" (moved from {_ks_label(removed[obj][0])})"
            lines.append(f"{sign} {_obj_label(obj)}{note}" + "".join(f"\n    {c}" for c in changes))
    return lines


def _describe(result: Result) -> str:
    match result:
        case Rendered(objects):
            return f"rendered {len(objects)} objects"
        case Failed(error):
            return "build failed: " + error.splitlines()[-1][:200] if error else "build failed"
        case NotRendered(reason):
            return f"not rendered: {reason}"


def _default_cache() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "render-diff"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base", help="base revision")
    parser.add_argument("head", help="head revision")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="ducktape checkout (default: cwd)")
    parser.add_argument("--cache-dir", type=Path, default=_default_cache())
    parser.add_argument("--jobs", type=int, default=2 * (os.cpu_count() or 1))
    parser.add_argument("--kustomize", default=shutil.which("kustomize") or "kustomize")
    args = parser.parse_args()
    started = time.monotonic()
    repo = Path(_git(args.repo, "rev-parse", "--show-toplevel").decode().strip())
    store = BlobStore(args.cache_dir / "blobs")
    renderer = Renderer(args.cache_dir, store, args.kustomize)
    graphs = []
    for rev in (args.base, args.head):
        sha = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").decode().strip()
        revision = Revision(repo, sha, git_tree(repo, sha), args.cache_dir / "upstream", store)
        graphs.append(reconcile(revision, renderer, args.jobs))
    base, head = graphs
    report = compare(base, head)
    print("\n".join(report))
    unrendered = sorted(f"{_ks_label(k)}: {r.reason}" for k, r in head.results.items() if isinstance(r, NotRendered))
    if unrendered:
        print(f"-- not rendered at head ({len(unrendered)}):", *unrendered, sep="\n   ", file=sys.stderr)
    print(
        f"-- {len(base.results)} base / {len(head.results)} head Kustomizations; "
        f"{renderer.builds} builds, {renderer.hits} cache hits; {time.monotonic() - started:.1f}s; "
        + ("DIFFERENT" if report else "identical"),
        file=sys.stderr,
    )
    if report:
        raise SystemExit(1)  # the contract: non-zero on any rendered difference


if __name__ == "__main__":
    main()
