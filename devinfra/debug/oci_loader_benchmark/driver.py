"""RBE comparison driver for the OCI loader investigation."""

import subprocess
import sys

from python.runfiles import Runfiles

from devinfra.oci.oci import (
    _already_loaded,
    _loaded_marker,
    _one_loader_at_a_time,
    daemon_image_id,
    load_oci_image,
    read_oci_layout_digest,
)
from third_party.containers import postgres_18


def main() -> None:
    mode, *args = sys.argv[1:]
    if mode == "noop":
        return
    tag, *script = args
    if mode == "python":
        load_oci_image(postgres_18.LAYOUT_RLOCATION, image_ref=tag)
        return
    if mode != "guarded_rules" or len(script) != 1:
        raise ValueError((mode, script))
    wanted = read_oci_layout_digest(Runfiles.CreateOrRaise().root() / postgres_18.LAYOUT_RLOCATION)
    if _already_loaded(tag, wanted):
        return
    with _one_loader_at_a_time(tag):
        if _already_loaded(tag, wanted):
            return
        subprocess.run([script[0]], check=True)
        _loaded_marker(tag).write_text(f"{wanted} {daemon_image_id(tag)}")


if __name__ == "__main__":
    main()
