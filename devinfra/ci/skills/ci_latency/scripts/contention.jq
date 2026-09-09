# Read the same snapshot as summarize.jq. Retrospective completed-job lower bounds.
def epoch: fromdateiso8601;
def assigned: (.runner_id // 0) > 0 and .started_at != null;
. as $s |
($s.runs | map({key:(.id|tostring),value:.path}) | from_entries) as $paths |
[$s.jobs[]|. + {workflow:$paths[.run_id|tostring]}] as $jobs |
{
  codeql_languages: ([$jobs[]|select(.workflow=="dynamic/github-code-scanning/codeql" and assigned and .completed_at != null and .created_at >= $since and .created_at <= $until)] |
    group_by(.name)|map({name:.[0].name,jobs:length,
      runner_seconds:([.[]|(.completed_at|epoch)-(.started_at|epoch)]|add)})),
  bazel_jobs: [$jobs[]|select(.name|endswith("Test & Build"))|
    select(assigned and .created_at >= $since and .created_at <= $until)|
    {html_url,status,conclusion,created_at,started_at,completed_at,
     queue_seconds:((.started_at|epoch)-(.created_at|epoch)),
     runtime_seconds:(if .completed_at then ((.completed_at|epoch)-(.started_at|epoch)) else null end)}],
  timeline: [range(($since|epoch);($until|epoch);60) as $t |
    [$jobs[]|select(assigned and .completed_at != null)|
      select((.started_at|epoch)<=$t and (.completed_at|epoch)>$t)] as $active |
    {at:($t|todateiso8601),occupied:($active|length),
     codeql:([$active[]|select(.workflow=="dynamic/github-code-scanning/codeql")]|length),
     bazel_waiting:([$jobs[]|select(.name|endswith("Test & Build"))|
       select(assigned and (.created_at|epoch)<=$t and (.started_at|epoch)>$t)|.html_url])}]
}
