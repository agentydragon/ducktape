import re
import shlex
import subprocess
import sys

# Pass arguments to rcup, check it didn't ask for confirmation.
# Run rcup with a pipe to capture output
args = ["rcup", *sys.argv[1:]]
proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

# Give it a moment to either complete or start prompting
try:
    output, errors = proc.communicate(timeout=2)
    print(output)
    print(errors, file=sys.stderr)
    sys.exit(proc.returncode)

except subprocess.TimeoutExpired:
    # Still running after timeout - likely waiting for input
    proc.kill()
    output, errors = proc.communicate()

    if re.search(r"overwrite .+\? \[ynaq\]", output + errors):
        print("rcup interactively asked whether to overwrite, you should run it manually:")
    else:
        print("rcup appears to be waiting for input (timed out)")
    print("    " + shlex.join(args))

    print(output)
    print(errors, file=sys.stderr)

    sys.exit(1)
