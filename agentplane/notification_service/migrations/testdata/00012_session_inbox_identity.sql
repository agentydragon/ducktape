-- +goose Up
-- Do not choose or discard an inbox when legacy destination-scoped Session IDs collide.
-- The migration fails transactionally until those records have been reconciled.
CREATE UNIQUE INDEX inbox_session_id_unique ON inbox (session_id);

-- +goose StatementBegin
CREATE FUNCTION notify_session_inbox_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    affected_session text;
    affected_inbox uuid;
BEGIN
    IF TG_TABLE_NAME = 'inbox' THEN
        affected_session := CASE WHEN TG_OP = 'DELETE' THEN OLD.session_id ELSE NEW.session_id END;
    ELSE
        affected_inbox := CASE WHEN TG_OP = 'DELETE' THEN OLD.inbox_id ELSE NEW.inbox_id END;
        SELECT session_id INTO affected_session FROM inbox WHERE id = affected_inbox;
    END IF;
    IF affected_session IS NOT NULL THEN
        PERFORM pg_notify('agentplane_notification_inbox', affected_session);
    END IF;
    RETURN NULL;
END $$;
-- +goose StatementEnd

CREATE TRIGGER inbox_watch_change AFTER INSERT OR UPDATE OR DELETE ON inbox
    FOR EACH ROW EXECUTE FUNCTION notify_session_inbox_change();
CREATE TRIGGER entry_watch_change AFTER INSERT OR UPDATE OR DELETE ON entry
    FOR EACH ROW EXECUTE FUNCTION notify_session_inbox_change();
CREATE TRIGGER notice_watch_change AFTER INSERT OR UPDATE OR DELETE ON notice
    FOR EACH ROW EXECUTE FUNCTION notify_session_inbox_change();
CREATE TRIGGER subscription_watch_change AFTER INSERT OR UPDATE OR DELETE ON subscription
    FOR EACH ROW EXECUTE FUNCTION notify_session_inbox_change();

-- Shared GitHub observations can change subscription status for several inboxes at once.
-- +goose StatementBegin
CREATE FUNCTION notify_github_subscription_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    changed_repository bigint;
    changed_kind text;
    changed_subject text;
    changed_app bigint;
    changed_installation bigint;
    affected_session text;
BEGIN
    IF TG_TABLE_NAME = 'github_installation' THEN
        changed_app := CASE WHEN TG_OP = 'DELETE' THEN OLD.app_id ELSE NEW.app_id END;
        changed_installation := CASE WHEN TG_OP = 'DELETE' THEN OLD.installation_id ELSE NEW.installation_id END;
        FOR affected_session IN
            SELECT DISTINCT i.session_id FROM inbox i JOIN subscription s ON s.inbox_id = i.id
            WHERE s.github_app_id = changed_app AND (s.github_installation_id = changed_installation OR EXISTS (
                SELECT 1 FROM github_subject_revision r JOIN github_repository_access a
                    ON a.repository_id = r.head_repository_id AND a.app_id = changed_app
                       AND a.installation_id = changed_installation
                WHERE r.repository_id = s.github_repository_id AND r.kind = s.github_subject_kind
                  AND r.subject_key = s.github_subject_key))
        LOOP
            PERFORM pg_notify('agentplane_notification_inbox', affected_session);
        END LOOP;
    ELSIF TG_TABLE_NAME = 'github_repository_access' THEN
        changed_repository := CASE WHEN TG_OP = 'DELETE' THEN OLD.repository_id ELSE NEW.repository_id END;
        changed_app := CASE WHEN TG_OP = 'DELETE' THEN OLD.app_id ELSE NEW.app_id END;
        FOR affected_session IN
            SELECT DISTINCT i.session_id FROM inbox i JOIN subscription s ON s.inbox_id = i.id
            WHERE s.github_app_id = changed_app AND
                (s.github_repository_id = changed_repository OR EXISTS (
                    SELECT 1 FROM github_subject_revision r
                    WHERE r.repository_id = s.github_repository_id AND r.kind = s.github_subject_kind
                      AND r.subject_key = s.github_subject_key AND r.head_repository_id = changed_repository))
        LOOP
            PERFORM pg_notify('agentplane_notification_inbox', affected_session);
        END LOOP;
    ELSE
        changed_repository := CASE WHEN TG_OP = 'DELETE' THEN OLD.repository_id ELSE NEW.repository_id END;
        IF TG_TABLE_NAME IN ('github_subject', 'github_subject_revision') THEN
            changed_kind := CASE WHEN TG_OP = 'DELETE' THEN OLD.kind ELSE NEW.kind END;
            changed_subject := CASE WHEN TG_OP = 'DELETE' THEN OLD.subject_key ELSE NEW.subject_key END;
        ELSE
            changed_kind := CASE WHEN TG_OP = 'DELETE' THEN OLD.subject_kind ELSE NEW.subject_kind END;
            changed_subject := CASE WHEN TG_OP = 'DELETE' THEN OLD.subject_key ELSE NEW.subject_key END;
        END IF;
        FOR affected_session IN
            SELECT DISTINCT i.session_id FROM inbox i JOIN subscription s ON s.inbox_id = i.id
            WHERE s.github_repository_id = changed_repository AND s.github_subject_kind = changed_kind
              AND s.github_subject_key = changed_subject
        LOOP
            PERFORM pg_notify('agentplane_notification_inbox', affected_session);
        END LOOP;
    END IF;
    RETURN NULL;
END $$;
-- +goose StatementEnd

CREATE TRIGGER github_installation_watch_change AFTER INSERT OR UPDATE OR DELETE ON github_installation
    FOR EACH ROW EXECUTE FUNCTION notify_github_subscription_change();
CREATE TRIGGER github_access_watch_change AFTER INSERT OR UPDATE OR DELETE ON github_repository_access
    FOR EACH ROW EXECUTE FUNCTION notify_github_subscription_change();
CREATE TRIGGER github_subject_watch_change AFTER INSERT OR UPDATE OR DELETE ON github_subject
    FOR EACH ROW EXECUTE FUNCTION notify_github_subscription_change();
CREATE TRIGGER github_subject_revision_watch_change AFTER INSERT OR UPDATE OR DELETE ON github_subject_revision
    FOR EACH ROW EXECUTE FUNCTION notify_github_subscription_change();
CREATE TRIGGER github_commit_refresh_watch_change AFTER INSERT OR UPDATE OR DELETE ON github_commit_refresh
    FOR EACH ROW EXECUTE FUNCTION notify_github_subscription_change();
CREATE TRIGGER github_review_refresh_watch_change AFTER INSERT OR UPDATE OR DELETE ON github_review_refresh
    FOR EACH ROW EXECUTE FUNCTION notify_github_subscription_change();
