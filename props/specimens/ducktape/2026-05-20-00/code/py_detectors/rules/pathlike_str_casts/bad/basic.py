import subprocess
from pathlib import Path


def bad():
    subprocess.run(["echo", str(Path("/etc/hosts"))], check=False)
