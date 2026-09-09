# Usage: jq --arg since UTC --arg until UTC -f summarize.jq snapshot.json
# Nearest-rank percentiles; seconds throughout. Workflow path, not dynamic run title.
def epoch: fromdateiso8601;
def stats:
  sort | if length == 0 then {n: 0} else
    {n: length, p50: .[((length * 0.50 | ceil) - 1)],
     p90: .[((length * 0.90 | ceil) - 1)], max: max, sum: add} end;
def assigned: (.runner_id // 0) > 0 and .started_at != null;
. as $s |
($since | epoch) as $lo | ($until | epoch) as $hi |
($s.runs | map({key:(.id|tostring),value:.}) | from_entries) as $runs |
[$s.jobs[] | . + {workflow: $runs[.run_id|tostring].path,
  event: $runs[.run_id|tostring].event} |
  . + {queue_seconds: (if assigned then ((.started_at|epoch)-(.created_at|epoch)) else null end),
  runtime_seconds: (if assigned and .completed_at != null then
    ((.completed_at|epoch)-(.started_at|epoch)) else null end)}] as $jobs |
{
  window: {since:$since, until:$until}, collected_at:$s.collected_at,
  run_count: ($s.runs|length), job_count: ($jobs|length),
  workflows: [$jobs | group_by(.workflow)[] | {
    workflow: .[0].workflow, jobs:length,
    status: (group_by(.status)|map({key:.[0].status,value:length})|from_entries),
    queue: ([.[]|select(.queue_seconds != null and .created_at >= $since and .created_at <= $until)|.queue_seconds]|stats),
    runtime: ([.[]|select(.runtime_seconds != null and .created_at >= $since and .created_at <= $until)|.runtime_seconds]|stats),
    occupied_seconds_in_window: ([.[]|select(assigned and .completed_at != null)|
      ([ (.completed_at|epoch), $hi ]|min) - ([ (.started_at|epoch), $lo ]|max) | select(.>0)]|add // 0)
  }],
  # End-before-start ordering prevents adjacent jobs counting as overlap.
  occupancy: ([$jobs[]|select(assigned and .completed_at != null)|
    ([ (.started_at|epoch), $lo ]|max) as $start |
    ([ (.completed_at|epoch), $hi ]|min) as $end |
    select($end > $start)|{t:$start,d:1},{t:$end,d:-1}] |
    sort_by(.t,.d) | reduce .[] as $e ({current:0,peak:0};
      .current += $e.d | .peak = ([.peak,.current]|max)) | {completed_job_peak:.peak}),
  longest_jobs: ([$jobs[]|select(.runtime_seconds != null)]|sort_by(-.runtime_seconds)|.[:15]|
    map({workflow,name,html_url,queue_seconds,runtime_seconds})),
  longest_queues: ([$jobs[]|select(.queue_seconds != null)]|sort_by(-.queue_seconds)|.[:15]|
    map({workflow,name,html_url,queue_seconds,runtime_seconds})),
  steps: ([$jobs[]|select(.created_at >= $since and .created_at <= $until)|. as $job|
    .steps[]|select(.status=="completed" and .conclusion!="skipped" and .started_at != null and .completed_at != null)|
    {workflow:$job.workflow,name,seconds:((.completed_at|epoch)-(.started_at|epoch))}] |
    group_by([.workflow,.name])|map({workflow:.[0].workflow,name:.[0].name,seconds:(map(.seconds)|stats)}))
}
