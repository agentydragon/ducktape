# jq -s -f checks.jq SNAPSHOT_DIRECTORY/checks/*.json
# These are visible latest head checks, not necessarily required merge gates.
[.[]|.[].check_runs[]] | group_by(.name) | map({
  name:.[0].name, n:length,
  pending:([.[]|select(.status!="completed")]|length),
  conclusions:(group_by(.conclusion)|map({key:(.[0].conclusion // "unfinished"),value:length})|from_entries)
})
