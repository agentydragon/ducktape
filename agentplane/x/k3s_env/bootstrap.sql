-- The fixture entrypoint creates only the fixture_admin bootstrap account and postgres database.
\getenv app_password AGENTPLANE_DB_PASSWORD_APP
CREATE ROLE app WITH LOGIN PASSWORD :'app_password';
CREATE DATABASE app OWNER app;
\getenv actions_password AGENTPLANE_DB_PASSWORD_ACTIONS
CREATE ROLE actions WITH LOGIN PASSWORD :'actions_password';
CREATE DATABASE actions OWNER actions;
\getenv egress_password AGENTPLANE_DB_PASSWORD_EGRESS
CREATE ROLE egress WITH LOGIN PASSWORD :'egress_password';
CREATE DATABASE egress OWNER egress;
\getenv notifications_password AGENTPLANE_DB_PASSWORD_NOTIFICATIONS
CREATE ROLE notifications WITH LOGIN PASSWORD :'notifications_password';
CREATE DATABASE notifications OWNER notifications;
\getenv sandbox_service_password AGENTPLANE_DB_PASSWORD_SANDBOX_SERVICE
CREATE ROLE sandbox_service WITH LOGIN PASSWORD :'sandbox_service_password';
CREATE DATABASE sandbox_service OWNER sandbox_service;
\getenv session_dispatcher_password AGENTPLANE_DB_PASSWORD_SESSION_DISPATCHER
CREATE ROLE session_dispatcher WITH LOGIN PASSWORD :'session_dispatcher_password';
CREATE DATABASE session_dispatcher OWNER session_dispatcher;
CREATE ROLE electric;
