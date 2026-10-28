# GetExecution includes work reused from another invocation (MERGED = 2).
# Count only originating (NEW = 1) test actions with complete process clocks.
def seconds:
  capture("^(?<whole>[^.]+)(?:\\.(?<fraction>[0-9]+))?Z$")
  | ((.whole + "Z" | fromdateiso8601)
     + (if .fraction then ("0." + .fraction | tonumber) else 0 end));

[.execution[]?
 | select(.invocationLinkType == 1 and .actionMnemonic == "TestRunner")
 | . as $action | .executedActionMetadata as $clock
 | select($clock.executionStartTimestamp and $clock.executionCompletedTimestamp)
 | {label: .targetLabel,
    process_seconds: (($clock.executionCompletedTimestamp | seconds)
                      - ($clock.executionStartTimestamp | seconds)),
    cpu_seconds: (if $clock.usageStats.cpuNanos then
                    ($clock.usageStats.cpuNanos | tonumber) / 1e9 else null end)}]
| group_by(.label)
| map({label: .[0].label, fresh_actions: length,
       process_seconds: (map(.process_seconds) | add),
       longest_action_seconds: (map(.process_seconds) | max),
       cpu_seconds: ([.[].cpu_seconds | select(. != null)] | add),
       missing_cpu_actions: (map(select(.cpu_seconds == null)) | length)})
| sort_by(-.process_seconds)
