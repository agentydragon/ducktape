-- +goose Up
-- The historical NO_OP writer settled a queued stop_runner_session while its Sandbox was
-- suspended and supplied no runner admission. Keep the strongest fact still present on each row:
-- a recorded runner admission proves RUNNER_ADMITTED; without one, use UNKNOWN.
UPDATE dispatch
SET state = CASE
    WHEN admission IS NOT NULL THEN 'RUNNER_ADMITTED'
    ELSE 'UNKNOWN'
END
WHERE state IN ('NO_OP', 'HARNESS_CONFIRMED');
