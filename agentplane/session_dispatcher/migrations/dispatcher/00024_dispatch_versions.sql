-- +goose Up
CREATE SEQUENCE dispatch_version_seq AS BIGINT;

ALTER TABLE dispatch ADD COLUMN version BIGINT;
UPDATE dispatch SET version = nextval('dispatch_version_seq');
ALTER TABLE dispatch ALTER COLUMN version SET NOT NULL;
CREATE UNIQUE INDEX ix_dispatch_version ON dispatch (version);

-- Receipt evidence only: no Sandbox address or queued command column.
-- Session records retain the read owner and scope after Environment deletion.
CREATE TABLE dispatch_tombstone (
    command_id VARCHAR NOT NULL,
    version BIGINT NOT NULL,
    session_id VARCHAR NOT NULL,
    operation VARCHAR NOT NULL,
    state VARCHAR NOT NULL,
    reason VARCHAR,
    admission BYTEA,
    delivery_blocker BYTEA,
    wake_assessment BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (command_id, version)
);
CREATE UNIQUE INDEX ix_dispatch_tombstone_version ON dispatch_tombstone (version);
CREATE INDEX ix_dispatch_tombstone_deleted_at ON dispatch_tombstone (deleted_at);

-- Pruning must retain evidence that a cursor can no longer cover every deletion.
CREATE TABLE dispatch_watch_retention (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    pruned_through BIGINT NOT NULL DEFAULT 0 CHECK (pruned_through >= 0)
);
INSERT INTO dispatch_watch_retention DEFAULT VALUES;

-- Triggers also cover replicas running the image from before this migration.
-- Sequence allocation order is not commit order: a resumable reader must first
-- establish a stable checkpoint excluding in-flight dispatch/tombstone writes.
-- +goose StatementBegin
CREATE FUNCTION dispatch_assign_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.version := nextval('dispatch_version_seq');
    RETURN NEW;
END
$$;
-- +goose StatementEnd
CREATE TRIGGER dispatch_assign_version BEFORE INSERT OR UPDATE ON dispatch
    FOR EACH ROW EXECUTE FUNCTION dispatch_assign_version();

-- +goose StatementBegin
CREATE FUNCTION dispatch_retain_deletion() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO dispatch_tombstone (
        command_id,version,session_id,operation,state,reason,admission,
        delivery_blocker,wake_assessment,created_at,updated_at
    ) VALUES (
        OLD.command_id,nextval('dispatch_version_seq'),OLD.session_id,OLD.operation,OLD.state,OLD.reason,OLD.admission,
        OLD.delivery_blocker,OLD.wake_assessment,OLD.created_at,OLD.updated_at
    );
    RETURN OLD;
END
$$;
-- +goose StatementEnd
CREATE TRIGGER dispatch_retain_deletion AFTER DELETE ON dispatch
    FOR EACH ROW EXECUTE FUNCTION dispatch_retain_deletion();
