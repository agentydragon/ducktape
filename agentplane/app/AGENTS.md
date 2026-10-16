# Thread browser tests

The first visit to a seeded thread bootstraps its Electric shape before the tail rows appear. Keep
the tail-readiness check observable and bounded at 30 seconds, as in
`test_returning_to_a_thread_lays_its_rows_out_at_the_heights_they_had`; do not replace it with a
sleep or raise the Bazel target timeout.
