\getenv central_sampler_password AGENTPLANE_DB_PASSWORD_CENTRAL_SAMPLER
CREATE ROLE central_sampler WITH LOGIN PASSWORD :'central_sampler_password';
CREATE DATABASE central_sampler OWNER central_sampler;
