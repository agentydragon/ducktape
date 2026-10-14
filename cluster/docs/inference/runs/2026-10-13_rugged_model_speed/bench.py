import json
import random
import subprocess
import sys
import time
import urllib.request

H = "http://127.0.0.1:11500"
words = [
    "alpha",
    "beta",
    "gamma",
    "delta",
    "epsilon",
    "zeta",
    "eta",
    "theta",
    "iota",
    "kappa",
    "lambda",
    "mu",
    "nu",
    "xi",
    "omicron",
    "pi",
    "rho",
    "sigma",
    "tau",
    "upsilon",
]
random.seed(1)


def post(path, body):
    req = urllib.request.Request(H + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=1800))


def rss_gb():
    out = subprocess.run(
        "ps -eo rss,args | grep -E 'llama-server|ollama runner' | grep -v grep | awk '{s+=$1} END{print s/1048576}'",
        shell=True,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return float(out or 0)


# usage: bench.py MODEL NUM_CTX PROMPT_TOKENS[,PROMPT_TOKENS...]
model, ctx, sizes = sys.argv[1], int(sys.argv[2]), [int(x) for x in sys.argv[3].split(",")]
post("/api/generate", {"model": model, "keep_alive": 0})
time.sleep(2)
t = time.time()
post(
    "/api/chat",
    {
        "model": model,
        "stream": False,
        "think": False,
        "keep_alive": "5m",
        "messages": [{"role": "user", "content": "hi"}],
        "options": {"num_ctx": ctx, "num_predict": 4},
    },
)
loaded = json.load(urllib.request.urlopen(H + "/api/ps"))["models"][0]
print(
    f"cold_load_s={time.time() - t:.1f} size_vram_gib={loaded['size_vram'] / 2**30:.2f} ctx={loaded['context_length']}"
)
for n in sizes:
    prompt = "Summarize the following text in one sentence.\n\n" + " ".join(
        random.choice(words) for _ in range(int(n * 0.75))
    )
    t = time.time()
    d = post(
        "/api/chat",
        {
            "model": model,
            "stream": False,
            "think": False,
            "keep_alive": "5m",
            "messages": [{"role": "user", "content": prompt}],
            "options": {"num_ctx": ctx, "num_predict": 128, "seed": 1},
        },
    )
    print(
        f"prompt={d['prompt_eval_count']} prefill_tps={d['prompt_eval_count'] / d['prompt_eval_duration'] * 1e9:.1f} "
        f"decode_tps={d['eval_count'] / d['eval_duration'] * 1e9:.1f} wall_s={time.time() - t:.1f} rss_gb={rss_gb():.2f}"
    )
post("/api/generate", {"model": model, "keep_alive": 0})
