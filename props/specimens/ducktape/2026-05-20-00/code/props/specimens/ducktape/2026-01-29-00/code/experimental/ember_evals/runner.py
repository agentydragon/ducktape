from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from experimental.ember_evals.common import CommandError, dump_yaml, merge_dict, run_command
from experimental.ember_evals.kubernetes import KubernetesManager, LabelSelector
from experimental.ember_evals.matrix import MatrixHarness, MatrixTranscript, render_matrix_transcript
from experimental.ember_evals.models import (
    EvalRunErrorReport,
    EvalRunMetadata,
    EvalRunRequest,
    RunLabels,
    RuntimeSecretNames,
)
from experimental.ember_evals.steps import ScenarioSuiteResult

from ember.integrations.gitea import GiteaRepository
from experimental.ember_evals.definitions import ScenarioSuite
from experimental.ember_evals.executor import ScenarioExecutor
from experimental.ember_evals.scenarios.regression import SCENARIO_SUITE as REGRESSION_SUITE


def _get_repo_root() -> Path:
    """Get repository root from environment or discover it."""
    if repo_root := os.environ.get("DUCKTAPE_REPO_ROOT"):
        return Path(repo_root)

    # Fall back to discovery from known paths
    current_file = Path(__file__).resolve()
    for parent in current_file.parents:
        if (parent / "k8s" / "helm" / "ember").exists():
            return parent

    raise RuntimeError(
        "Cannot locate ducktape repository root. Set DUCKTAPE_REPO_ROOT environment variable "
        "or ensure this runs from within the ducktape repository."
    )


REPO_ROOT = _get_repo_root()
CHART_PATH = REPO_ROOT / "k8s" / "helm" / "ember"
BASE_VALUES_FILE = CHART_PATH / "values-eval.yaml"
ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "ember-eval"
DEFAULT_IMAGE_REPOSITORY = "registry.k3s.agentydragon.com/emberd"


@dataclass(frozen=True)
class SuiteOption:
    description: str
    suite: ScenarioSuite


SUITE_REGISTRY: dict[str, SuiteOption] = {
    "regression": SuiteOption(description="Baseline regression coverage for Ember", suite=REGRESSION_SUITE)
}


def suite_keys() -> Iterable[str]:
    return SUITE_REGISTRY.keys()


def get_suite_option(key: str) -> SuiteOption:
    try:
        return SUITE_REGISTRY[key]
    except KeyError as exc:
        allowed = ", ".join(suite_keys())
        raise CommandError(f"Unknown evaluation suite '{key}'. Available suites: {allowed}") from exc


def ensure_tools_available(build_required: bool) -> None:
    tools = ["helm"]
    if build_required:
        tools.append("docker")
    for tool in tools:
        if shutil.which(tool) is None:
            raise RuntimeError(f"{tool} not found in PATH")


def sanitize_for_k8s(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9-]", "-", value.lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if not cleaned:
        cleaned = fallback
    if cleaned[0].isdigit():
        cleaned = f"a-{cleaned}"
    return cleaned[:63]


def resource_name(base: str, suffix: str, fallback: str) -> str:
    base_clean = sanitize_for_k8s(base, fallback)
    suffix_clean = sanitize_for_k8s(suffix, suffix)
    max_base = 63 - len(suffix_clean) - 1
    max_base = max(max_base, 1)
    base_trimmed = base_clean[:max_base]
    return f"{base_trimmed}-{suffix_clean}"


async def helm_upgrade(release: str, namespace: str, values_file: Path) -> None:
    cmd = [
        "helm",
        "upgrade",
        "--install",
        release,
        str(CHART_PATH),
        "--namespace",
        namespace,
        "-f",
        str(BASE_VALUES_FILE),
        "-f",
        str(values_file),
        "--wait",
        "--timeout",
        "10m",
    ]
    await run_command(cmd)


async def helm_uninstall(release: str, namespace: str) -> None:
    await run_command(("helm", "uninstall", release, "--namespace", namespace), check=False)


def write_values_file(path: Path, payload: Mapping[str, object]) -> None:
    path.write_bytes(dump_yaml(payload))


def make_base_run_id(explicit: str | None) -> str:
    if explicit:
        return sanitize_for_k8s(explicit, "eval")
    timestamp = _dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return f"eval-{timestamp}"


async def git_output(*args: str) -> str:
    return (await run_command(args, cwd=REPO_ROOT)).stdout.strip()


async def compute_image_tag() -> str:
    timestamp = _dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    try:
        short_sha = await git_output("git", "rev-parse", "--short", "HEAD")
    except CommandError:
        short_sha = "nogit"
    dirty = ""
    try:
        if await git_output("git", "status", "--porcelain"):
            dirty = "d"
    except CommandError:
        dirty = "d"
    return f"{timestamp}-{short_sha}{dirty}"


async def build_image(image_ref: str) -> None:
    print(f"[ember-eval] Building image {image_ref} from working tree...")
    await run_command(("docker", "build", "-t", image_ref, "."), cwd=REPO_ROOT / "ember")
    print(f"[ember-eval] Pushing image {image_ref}...")
    await run_command(("docker", "push", image_ref))


def load_base_values() -> dict[str, object]:
    with BASE_VALUES_FILE.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def split_image_ref(image: str) -> tuple[str, str]:
    if ":" not in image:
        raise CommandError(f"Image reference '{image}' must include a tag")
    repository, tag = image.rsplit(":", 1)
    if not repository or not tag:
        raise CommandError(f"Invalid image reference '{image}'")
    return repository, tag


def prepare_values(
    *,
    namespace: str,
    matrix_base_url: str,
    rspcache_api_base: str,
    labels: RunLabels,
    release: str,
    secrets: RuntimeSecretNames,
    image: str,
) -> dict[str, object]:
    values = load_base_values()
    repository, tag = split_image_ref(image)
    overrides = {
        "namespace": {"create": False, "name": namespace},
        "config": {"matrix": {"base_url": matrix_base_url}, "openai": {"api_base": rspcache_api_base}},
        "podLabels": labels.pod_labels(release),
        "runtimeSecrets": {
            "matrixTokenSecret": secrets.matrix,
            "matrixAccessTokenKey": "access_token",
            "giteaSecret": secrets.gitea,
            "giteaTokenKey": "token",
            "rspcacheClientSecret": secrets.rspcache,
            "rspcacheApiKeyKey": "openai_api_key",
        },
        "image": {"repository": repository, "tag": tag},
    }
    merge_dict(values, overrides)
    return values


async def resolve_image(args: argparse.Namespace) -> str:
    repository = args.image_repository
    if args.image_tag:
        return f"{repository}:{args.image_tag}"
    tag = await compute_image_tag()
    image_ref = f"{repository}:{tag}"
    await build_image(image_ref)
    return image_ref


def write_artifact(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2), encoding="utf-8")


async def execute_run_async(request: EvalRunRequest) -> EvalRunMetadata:
    artifact_dir = request.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)

    metadata = EvalRunMetadata(
        run_id=request.run_id,
        namespace=request.namespace,
        release=request.release,
        image=request.image,
        suite_key=request.suite_key,
        suite_name=request.suite.name,
        suite_version=request.suite.version,
        labels=request.labels,
        secrets=request.secrets,
        started_at=_dt.datetime.utcnow().isoformat() + "Z",
        status="deploying",
    )

    scenario_summary = ScenarioSuiteResult()
    transcript = MatrixTranscript()

    kube = await KubernetesManager.create()
    namespaced = kube.scope(request.namespace)
    try:
        namespace_labels = request.labels.namespace_labels()
        await kube.ensure_namespace(request.namespace, namespace_labels)
        await namespaced.upsert_secret(
            request.secrets.matrix, {"access_token": request.matrix_access_token}, namespace_labels
        )
        await namespaced.upsert_secret(request.secrets.gitea, {"token": request.gitea_token}, namespace_labels)
        await namespaced.upsert_secret(
            request.secrets.rspcache, {"openai_api_key": request.rspcache_api_key}, namespace_labels
        )

        with tempfile.TemporaryDirectory(prefix=f"ember-eval-{request.run_id}-") as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            values_path = temp_dir / "values.yaml"

            values_payload = prepare_values(
                namespace=request.namespace,
                matrix_base_url=request.matrix_base_url,
                rspcache_api_base=request.rspcache_api_base,
                labels=request.labels,
                release=request.release,
                secrets=request.secrets,
                image=request.image,
            )
            write_values_file(values_path, values_payload)

            await helm_upgrade(request.release, request.namespace, values_path)
            await namespaced.wait_for_deployment_ready(request.release)

            selector = LabelSelector({"ember.run/id": request.run_id, "app.kubernetes.io/component": "agent"})
            pod_name = await namespaced.first_pod_name(selector)
            suite = request.suite

            if suite.scenarios:
                async with MatrixHarness(
                    base_url=request.matrix_base_url,
                    access_token=request.matrix_access_token,
                    ember_user_id=request.ember_user_id,
                    run_id=request.run_id,
                    artifact_dir=artifact_dir,
                    room_id=request.matrix_room_id,
                ) as matrix:
                    executor = ScenarioExecutor(
                        request=request, matrix=matrix, pod_name=pod_name, artifact_dir=artifact_dir, kube=namespaced
                    )
                    scenario_summary = await executor.run_suite(suite)
                    transcript = MatrixTranscript(events=matrix.transcript)
            else:
                print("[ember-eval] No scenarios provided; skipping scenario execution.")

        metadata.status = "ready"
        metadata.ready_at = _dt.datetime.utcnow().isoformat() + "Z"
        write_artifact(artifact_dir / "metadata.json", metadata.model_dump())
        if transcript.events:
            write_artifact(artifact_dir / "matrix_transcript.json", transcript.model_dump())
            (artifact_dir / "matrix_transcript.txt").write_text(render_matrix_transcript(transcript), encoding="utf-8")
        if scenario_summary.scenarios:
            write_artifact(artifact_dir / "scenarios.json", scenario_summary.model_dump())
        print(f"[ember-eval] Run {request.run_id} ready (namespace {request.namespace}).")
        return metadata
    except Exception as exc:
        metadata.status = "failed"
        metadata.failed_at = _dt.datetime.utcnow().isoformat() + "Z"
        metadata.error = str(exc)
        if transcript.events:
            write_artifact(artifact_dir / "matrix_transcript.json", transcript.model_dump())
            (artifact_dir / "matrix_transcript.txt").write_text(render_matrix_transcript(transcript), encoding="utf-8")
        if scenario_summary.scenarios:
            write_artifact(artifact_dir / "scenarios.json", scenario_summary.model_dump())

        error_report = EvalRunErrorReport(
            metadata=metadata, scenarios=scenario_summary if scenario_summary.scenarios else None
        )
        write_artifact(artifact_dir / "error.json", error_report.model_dump())
        raise
    finally:
        try:
            if request.preserve:
                print(
                    f"[ember-eval] Preserve flag set for {request.run_id}; namespace {request.namespace} left intact."
                )
            else:
                await helm_uninstall(request.release, request.namespace)
                await kube.delete_namespace(request.namespace)
                print(f"[ember-eval] Cleaned up namespace {request.namespace}.")
        finally:
            await kube.close()


def plan_runs(
    args: argparse.Namespace, image_ref: str, suite_key: str, suite_option: SuiteOption
) -> list[EvalRunRequest]:
    runs: list[EvalRunRequest] = []
    base_run_id = make_base_run_id(args.run_id)
    pad_width = len(str(args.runs))

    if args.runs > 1 and (args.namespace or args.release):
        raise ValueError("Cannot override namespace/release when running multiple evaluations.")
    if args.runs > 1 and args.matrix_room_id:
        raise ValueError("Cannot reuse a fixed Matrix room when running multiple evaluations.")
    repository = GiteaRepository.parse(args.gitea_repo)

    for index in range(args.runs):
        suffix = f"-{index + 1:0{pad_width}d}" if args.runs > 1 else ""
        run_id = f"{base_run_id}{suffix}"
        namespace = args.namespace or resource_name(run_id, "ns", "ember-eval")
        release = args.release or resource_name(run_id, "rls", "ember-eval")
        secrets = RuntimeSecretNames(
            matrix=resource_name(run_id, "matrix", "ember-eval"),
            gitea=resource_name(run_id, "gitea", "ember-eval"),
            rspcache=resource_name(run_id, "rspcache", "ember-eval"),
        )
        labels = RunLabels(run_id=run_id, image=image_ref)
        username = None
        if args.gitea_username:
            username = args.gitea_username.format(run_id=run_id)
        runs.append(
            EvalRunRequest(
                run_id=run_id,
                namespace=namespace,
                release=release,
                labels=labels,
                matrix_base_url=args.matrix_base_url,
                matrix_access_token=args.matrix_access_token,
                gitea_token=args.gitea_token,
                gitea_base_url=args.gitea_base_url,
                gitea_repo=repository,
                gitea_username=username,
                rspcache_api_base=args.rspcache_api_base,
                rspcache_api_key=args.rspcache_api_key,
                suite_key=suite_key,
                suite=suite_option.suite,
                preserve=args.preserve,
                artifact_dir=ARTIFACT_ROOT / run_id,
                secrets=secrets,
                image=image_ref,
                matrix_room_id=args.matrix_room_id,
                ember_user_id=args.ember_user_id,
            )
        )
    return runs


async def run_eval_async(args: argparse.Namespace) -> None:
    if args.runs < 1:
        raise ValueError("runs must be >= 1")
    if args.parallel < 1:
        raise ValueError("parallel must be >= 1")

    build_required = args.image_tag is None
    ensure_tools_available(build_required)

    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)

    suite_option = get_suite_option(args.suite)
    image_ref = await resolve_image(args)
    if build_required:
        print(f"[ember-eval] Built fresh image {image_ref}")
    else:
        print(f"[ember-eval] Using pre-built image {image_ref}")

    run_requests = plan_runs(args, image_ref, args.suite, suite_option)
    max_parallel = min(args.parallel, args.runs)
    semaphore = asyncio.Semaphore(max_parallel)

    async def _run(request: EvalRunRequest) -> EvalRunMetadata:
        async with semaphore:
            return await execute_run_async(request)

    results = await asyncio.gather(*(_run(request) for request in run_requests), return_exceptions=True)

    failures: list[tuple[str, str]] = []
    for request, result in zip(run_requests, results, strict=True):
        if isinstance(result, Exception):
            failures.append((request.run_id, str(result)))
            print(f"[ember-eval] Run {request.run_id} failed: {result}", file=sys.stderr)

    if failures:
        raise CommandError(f"{len(failures)} evaluation run(s) failed.")

    print("[ember-eval] All evaluation runs completed successfully.")


def run_eval(args: argparse.Namespace) -> None:
    asyncio.run(run_eval_async(args))


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hybrid Ember evaluation runner")
    parser.add_argument("--matrix-base-url", required=True, help="Matrix homeserver base URL")
    parser.add_argument("--matrix-access-token", required=True, help="Matrix access token for the evaluator user")
    parser.add_argument("--matrix-room-id", help="Existing Matrix room ID to reuse for evaluation")
    parser.add_argument("--ember-user-id", required=True, help="Matrix user ID for the Ember agent under test")
    parser.add_argument("--gitea-token", required=True, help="Gitea personal access token for the evaluator")
    parser.add_argument(
        "--gitea-base-url", required=True, help="Base URL of the Gitea instance (e.g. https://gitea.example.com)"
    )
    parser.add_argument("--gitea-repo", required=True, help="Owner/repo slug used for evaluation (e.g. eval/ember)")
    parser.add_argument("--gitea-username", help="Expected Gitea username for Ember's comments")
    parser.add_argument("--rspcache-api-base", required=True, help="RSPCache/OpenAI API base URL")
    parser.add_argument("--rspcache-api-key", required=True, help="API key used by Ember via RSPCache")
    parser.add_argument("--run-id", help="Explicit base run identifier (default: timestamp)")
    parser.add_argument("--namespace", help="Override namespace name (single run only)")
    parser.add_argument("--release", help="Override Helm release name (single run only)")
    parser.add_argument(
        "--image-repository",
        default=DEFAULT_IMAGE_REPOSITORY,
        help=f"Docker image repository (default: {DEFAULT_IMAGE_REPOSITORY})",
    )
    parser.add_argument("--image-tag", help="Docker image tag to deploy (default: build from working tree)")
    parser.add_argument("--preserve", action="store_true", help="Skip cleanup after each run")
    parser.add_argument("--runs", type=int, default=1, help="Number of evaluation runs to execute")
    parser.add_argument("--parallel", type=int, default=1, help="Maximum number of runs to execute in parallel")
    suite_choices = list(suite_keys())
    if not suite_choices:
        raise RuntimeError("No evaluation suites registered")
    parser.add_argument(
        "--suite", default=suite_choices[0], choices=suite_choices, help="Evaluation scenario suite to run"
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _build_arg_parser()
    return parser.parse_args(list(argv) if argv is not None else None)


def cli_main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        run_eval(args)
    except CommandError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 1
    return 0
