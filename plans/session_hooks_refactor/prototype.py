"""Driver: load profile.yaml, run the engine, print drained output + session env file."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from engine import Engine, Task


async def amain() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    profile_path = repo_root / "plans/session_hooks_refactor/profile.yaml"
    session_id = str(uuid.uuid4())
    session_dir = Path("/tmp/claude-prototype") / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    profile = yaml.safe_load(profile_path.read_text())

    engine = Engine(session_dir=session_dir)
    for entry in profile["tasks"]:
        engine.add(
            Task(
                name=entry["name"],
                shell=entry["shell"],
                requires=entry.get("requires", []),
                timeout=entry.get("timeout"),
                background=entry.get("background", False),
            )
        )

    engine.post("prototype starting")
    await engine.run()
    engine.post("prototype done")

    print("=" * 72)
    print(f"session_dir: {session_dir}")
    print("=" * 72)
    print("DRAIN (would be additional_context on first hook event):")
    print("-" * 72)
    print(engine.drain())
    print("=" * 72)
    print("SESSION ENV FILE (cat envs/*.env):")
    print("-" * 72)
    print(engine.session_env_content())
    print("=" * 72)
    print("TASK STATES:")
    print("-" * 72)
    for name, t in engine.tasks.items():
        print(f"  {name:<40s} {t.state:<10s} exit={t.exit_code}")
    print("=" * 72)
    bazelrc = session_dir / "bazelrc"
    if bazelrc.exists():
        print("bazelrc:")
        print("-" * 72)
        print(bazelrc.read_text(), end="")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
