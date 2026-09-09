([.timeline[] | select(.at=="2026-09-09T20:44:00Z")][0] |
  .occupied==20 and .codeql==12 and (.bazel_waiting|length)==1) and
([.bazel_jobs[] | select(.html_url|endswith("102638083065"))][0] |
  .queue_seconds==177 and .runtime_seconds==89) and
([.bazel_jobs[] | select(.html_url|endswith("102645478845"))][0] |
  .queue_seconds==525 and .runtime_seconds==null)
