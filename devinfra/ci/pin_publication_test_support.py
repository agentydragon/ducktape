import dataclasses
from types import SimpleNamespace

from devinfra.buildbuddy.ci.bes import Invocation, Output
from devinfra.ci.plan_releases import Release

SOURCE = "2" * 40
RBE = "registry/image@sha256:" + "a" * 64


def release(pkg: str = "bbr", targets: str = "//pkg:wheel", filenames: str = "wheel.whl", flags: str = "") -> Release:
    return Release(pkg, targets, filenames, "//pkg:test", flags, "false", "")


def output(target: str = "//pkg:wheel", filename: str = "wheel.whl", digest: str = "a" * 64) -> Output:
    return Output(target, f"bazel-out/bin/pkg/{filename}", "bytestream://cache/blob", digest, 1, "default")


def normal(outputs: list[Output] | None = None) -> Invocation:
    return Invocation(
        outputs=outputs if outputs is not None else [output()],
        test_status={"//unrelated:test": "FAILED"},
        finished=True,
        successful=True,
        command="build",
        requested_patterns=("//...",),
        explicit_cmd_line=(
            "--keep_going",
            "--config=rbe",
            "--config=ci",
            "--build_metadata=ROLE=CI",
            "--build_metadata=DISABLE_TARGET_TRACKING=false",
            f"--remote_default_exec_properties=container-image=docker://{RBE}",
            "",
        ),
        commit_sha=SOURCE,
    )


def own_build(row: Release, identity: str) -> Invocation:
    args = [
        "--config=buildbuddy_remote_cache",
        "--config=buildbuddy_bes_results_url",
        "--config=buildbuddy_bes_backend",
        "--config=rbe",
        "--config=ci",
        f"--remote_default_exec_properties=container-image=docker://{RBE}",
        f"--invocation_id={identity}",
        "--remote_header=<REDACTED>",
        "--remote_upload_local_results",
    ]
    if row.bazel_flags:
        args.append("--compilation_mode=opt")
    return dataclasses.replace(
        normal([output(target, filename) for target, filename in row.assets]),
        requested_patterns=tuple(row.targets.split()),
        explicit_cmd_line=(*args, ""),
    )


def run(source: str = SOURCE, identity: int = 200, attempt: int = 1, **overrides) -> SimpleNamespace:
    values = {
        "head_sha": source,
        "id": identity,
        "run_attempt": attempt,
        "head_branch": "devel",
        "event": "push",
        "path": ".github/workflows/ci.yml",
        "head_repository": SimpleNamespace(full_name="agentydragon/ducktape"),
        "status": "completed",
        "conclusion": "success",
    }
    return SimpleNamespace(**(values | overrides))
