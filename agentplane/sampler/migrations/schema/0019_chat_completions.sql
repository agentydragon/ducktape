-- +goose Up
ALTER TABLE sampler_conversation
    DROP CONSTRAINT sampler_conversation_api_check,
    ADD CONSTRAINT sampler_conversation_api_check
        CHECK (api IN ('responses', 'anthropic_messages', 'chat_completions'));

ALTER TABLE sampler_context
    DROP CONSTRAINT sampler_context_api_check,
    ADD CONSTRAINT sampler_context_api_check
        CHECK (api IN ('responses', 'anthropic_messages', 'chat_completions'));
