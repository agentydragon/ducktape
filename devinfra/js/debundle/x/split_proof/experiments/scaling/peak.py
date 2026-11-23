"""Run a command; print wall seconds and peak memory. Kill it if it grows past a memory limit.

    python3 peak.py [--limit-mb 12000] [--timeout 900] -- command …

The kernel keeps every term it evaluates, so a large `decide +kernel` can take most of the machine's memory.
Peak memory is the largest resident set of any process in the tree, sampled every 0.2 s.
Output: `<seconds> s, peak <MB> MB, <exit | killed: memory | killed: timeout>`.
"""

import argparse
import subprocess
import time

parser = argparse.ArgumentParser()
parser.add_argument("--limit-mb", type=int, default=12000)
parser.add_argument("--timeout", type=float, default=900)
parser.add_argument("command", nargs=argparse.REMAINDER)
args = parser.parse_args()
command = args.command[1:] if args.command[:1] == ["--"] else args.command


def tree_rss_mb(root):
    """Largest resident set (MB) among `root` and its descendants."""
    ps = subprocess.run(["ps", "-eo", "pid=,ppid=,rss="], capture_output=True, text=True, check=False).stdout
    rows = [tuple(int(x) for x in line.split()) for line in ps.splitlines() if line.strip()]
    children = {}
    for pid, ppid, _ in rows:
        children.setdefault(ppid, []).append(pid)
    rss = {pid: kb for pid, _, kb in rows}
    todo, seen = [root], []
    while todo:
        pid = todo.pop()
        seen.append(pid)
        todo.extend(children.get(pid, []))
    return max((rss.get(pid, 0) for pid in seen), default=0) // 1024


start = time.time()
child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
peak, outcome = 0, None
while child.poll() is None:
    time.sleep(0.2)
    peak = max(peak, tree_rss_mb(child.pid))
    if peak > args.limit_mb:
        outcome = "killed: memory"
    elif time.time() - start > args.timeout:
        outcome = "killed: timeout"
    if outcome:
        subprocess.run(["pkill", "-KILL", "-P", str(child.pid)], check=False)
        child.kill()
        child.wait()
        break
print(f"{time.time() - start:.1f} s, peak {peak} MB, {outcome or f'exit {child.returncode}'}")
