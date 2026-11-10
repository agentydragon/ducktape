import subprocess
from pathlib import Path


def ok():
    subprocess.run(["echo", Path("/etc/hosts")], check=False)
