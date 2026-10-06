"""Assemble and publish the runner image; registry credentials never enter Nix."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

IMAGE_ATTR = ".#buildbuddy-remote-runner-image"
BASE_ENV = "DUCKTAPE_RUNNER_BASE_IMAGE"


def command(*args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(args, text=True, env=env).strip()


def base_reference(root: Path) -> str:
    pin = json.loads((root / "devinfra/image_pins.json").read_text())["rbe_container"]
    return f"{pin['image']}@{pin['digest']}"


def image_environment(base_path: str) -> dict[str, str]:
    return dict(os.environ, **{BASE_ENV: base_path})


def derivation(base_path: str) -> str:
    return command("nix", "eval", "--impure", "--raw", f"{IMAGE_ATTR}.drvPath", env=image_environment(base_path))


def manifest_digest(layout: Path) -> str:
    manifest = subprocess.check_output(["skopeo", "inspect", "--raw", f"oci:{layout}:runner"])
    return "sha256:" + hashlib.sha256(manifest).hexdigest()


def verify_runtime_config(base_archive: str, layout: Path) -> None:
    base = json.loads(command("skopeo", "inspect", "--config", f"docker-archive:{base_archive}"))
    candidate = json.loads(command("skopeo", "inspect", "--config", f"oci:{layout}:runner"))
    for field in ("os", "architecture", "config"):
        if base[field] != candidate[field]:
            raise ValueError(
                f"assembled image changed the base runtime {field}: {base[field]!r} != {candidate[field]!r}"
            )


def build(root: Path, work: Path, *, check_reproducible: bool) -> dict[str, str]:
    work.mkdir(parents=True, exist_ok=True)
    base_ref = base_reference(root)
    print(f"Fetching pinned base {base_ref}", flush=True)
    base_tar = work / "runner-base.tar"
    command(
        "skopeo",
        "copy",
        "--insecure-policy",
        "--override-os",
        "linux",
        "--override-arch",
        "amd64",
        f"docker://{base_ref}",
        f"docker-archive:{base_tar}:runner-base:fixed",
    )
    base_path = command("nix", "store", "add-file", "--name", "runner-base.tar", str(base_tar))
    base_tar.unlink()
    command("nix-store", "--add-root", str(work / "base"), "--realise", base_path)
    image_drv = derivation(base_path)
    print(f"Assembling {image_drv}", flush=True)
    args = ["nix", "build", "--impure", IMAGE_ATTR, "--out-link", str(work / "image"), "--print-out-paths"]
    layout = Path(command(*args, env=image_environment(base_path)))
    archive = command(
        "nix",
        "build",
        "--impure",
        f"{IMAGE_ATTR}.archive",
        "--out-link",
        str(work / "archive"),
        "--print-out-paths",
        env=image_environment(base_path),
    )
    digest = manifest_digest(layout)
    verify_runtime_config(base_path, layout)
    if check_reproducible:
        # Force image assembly again, rather than accepting the first cached output.
        command(
            "nix",
            "build",
            "--impure",
            f"{IMAGE_ATTR}.archive",
            "--no-link",
            "--rebuild",
            env=image_environment(base_path),
        )
        command(*args, "--rebuild", env=image_environment(base_path))
        repeated_digest = manifest_digest(layout)
        if repeated_digest != digest:
            raise ValueError(f"image assembly is not reproducible: {digest} != {repeated_digest}")
        print(f"Repeated image assembly produced {digest}")
    state = {
        "base_ref": base_ref,
        "base_path": base_path,
        "derivation": image_drv,
        "archive": archive,
        "layout": str(layout),
        "digest": digest,
    }
    (work / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    return state


def verify(root: Path, state: dict[str, str]) -> None:
    if base_reference(root) != state["base_ref"]:
        raise ValueError("RBE base pin changed since the image was built")
    if derivation(state["base_path"]) != state["derivation"]:
        raise ValueError("runner image derivation changed since the image was built")


def needs_publish(root: Path, state: dict[str, str], image: str) -> bool:
    pin = json.loads((root / "devinfra/image_pins.json").read_text())["buildbuddy_remote_runner"]
    return (pin["image"], pin["digest"]) != (image, state["digest"])


def publish(state: dict[str, str], image: str, tag: str) -> None:
    # Preserve the exact OCI manifest compared before publication, including layers.
    command(
        "skopeo",
        "copy",
        "--insecure-policy",
        "--preserve-digests",
        f"oci:{state['layout']}:runner",
        f"docker://{image}:{tag}",
    )
    actual = subprocess.check_output(["skopeo", "inspect", "--raw", f"docker://{image}:{tag}"])
    if "sha256:" + hashlib.sha256(actual).hexdigest() != state["digest"]:
        raise ValueError("published manifest differs from the assembled image")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build")
    make.add_argument("--work-dir", type=Path, required=True)
    make.add_argument("--image", required=True)
    make.add_argument("--github-output", type=Path)
    make.add_argument("--check-reproducible", action="store_true")
    for name in ("verify", "publish"):
        subparser = commands.add_parser(name)
        subparser.add_argument("--state", type=Path, required=True)
        if name == "publish":
            subparser.add_argument("--image", required=True)
            subparser.add_argument("--tag", required=True)
    args = parser.parse_args()
    root = Path.cwd()
    if args.command == "build":
        state = build(root, args.work_dir.resolve(), check_reproducible=args.check_reproducible)
        changed = needs_publish(root, state, args.image)
        print(f"Runner image {state['digest']}: {'publish required' if changed else 'already pinned'}")
        if args.github_output:
            with args.github_output.open("a") as output:
                output.write(
                    f"digest={state['digest']}\nneeds_publish={str(changed).lower()}\narchive={state['archive']}\n"
                )
    else:
        state = json.loads(args.state.read_text())
        verify(root, state)
        if args.command == "publish":
            publish(state, args.image, args.tag)


if __name__ == "__main__":
    main()
