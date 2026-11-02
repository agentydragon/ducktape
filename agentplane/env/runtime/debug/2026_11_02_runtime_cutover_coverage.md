# Runtime cutover test relocation

The old Host test target is removed together with the deleted Host implementation. Behavioral
tests move to Runtime's existing race-enabled target, with real control WatchState and incoming
authorization metadata. Native component fixtures explicitly supply a Runtime-owned idle delivery
worker so tests control exact ACK publication; ingress_owner_test retains real delivery coverage.

| Previous file | Destination / preserved behavior |
| --- | --- |
| host_test.go | session_admission_test.go: lazy/idempotent reservation, confirmed config, failed publication retry, interrupted setup recovery, input validation, operation-publication errors, late model lookup, script streams/cleanup |
| lifecycle_test.go | session_lifecycle_test.go: setup/admission drain, exact ACK, stale status projection, failed desired write and admission accounting, journal notifications, replacement after teardown |
| lifecycle_interleavings_test.go | session_interleavings_test.go: all admission/setup/Close/replacement/ACK orderings; Runtime Close now also requires the exact sealed ACK |
| process_launch_test.go | session_process_test.go: native exit cleanup, canceled wait retaining ownership, finalizer fence, Runtime-process exclusion from Session drain |
| transport_test.go | session_delivery_test.go: retained journal stream, bearer auth, under-cursor rejection, exact sealed ACK persistence |
| shutdown_test.go | session_shutdown_test.go: caller cancellation cannot cancel cleanup, engine failure cannot claim cleanup success |
| codex_app_server_controller_test.go | codexappserver/readiness_test.go: joined startup demand, native readiness refusal cleanup, fixed readiness deadline. Uses the real component's readiness dependency rather than a deleted RuntimeBridge. |
| config_test.go | session_config_test.go already moved by Session lane |
| fixtures_test.go, shared_engine_test.go, main_test.go | Removed Host-only constructors, fake RuntimeBridge, synthetic binding contexts and duplicate TestMain. Runtime fixtures own the single actual engine and supervisor helper. |
| intent_controller_test.go | Deleted intent-application protocol. Runtime admission_race_test covers failed desired publication, publication/acceptance interval and registered-operation drain accounting. host_exit_test covers startup recovery; close_intent_race_test covers Close versus Running preparation. |
| host_intent_close_test.go | Startup-failure gate and interrupted Close move to Runtime host_exit_test, close_intent_race_test and admission_race_test. No duplicate Host lifetime. |
| runtime_client_test.go | Generation/admission and exact Execution fence move to Runtime admission_race_test, direct_host_test, protocol_wait_test and existing Execution tests. Bootstrap accepted-work contract stays in Runtime aggregate_test plus direct regression. Host report projection/wakeups replaced by direct Session status/facts tests. |
| report_controller_test.go | Removed protobuf-equivalence/no-op publication test because HostReports and its writer no longer exist. Real status wakeup behavior remains in direct_host_test/protocol_wait_test. |
| lifecycle helper watch tests | Deleted tests for removed Host-local watchLifecycle wrapper; real lifecycle stage waiters still exercise resource creation and status wakeups. |

This is a migration ledger, not a claim that the resulting suite passes. Compiler and behavior
results are recorded separately as the integrated branch is tested.
