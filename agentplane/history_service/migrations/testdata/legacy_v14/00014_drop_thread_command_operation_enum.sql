-- +goose Up
-- Every History replica runs 00012 or later, so no writer spells a command operation by its old field name
-- and the rewrite trigger 00012 installed for the rollout has nothing left to do.
DROP TRIGGER thread_entity_command_operation_enum ON thread_entity;
DROP FUNCTION thread_command_operation_enum();
