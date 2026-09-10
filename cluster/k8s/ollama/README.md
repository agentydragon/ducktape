# Ollama direct bearer token

`secrets/ollama-direct-token-eso.yaml` uses ESO's `Password` generator to
create `ollama/ollama-direct-token`. The generated target is retained and is
not periodically rotated (`refreshInterval: 8760h`).

Reflector copies the Secret to `claude-sandbox`. Reloader restarts Ollama when
the token changes because nginx reads it from an environment variable. The
Deployment uses `Recreate`, so a deliberate rotation includes a service
interruption. Clients that cached the previous bearer token must fetch the new
value from `claude-sandbox/ollama-direct-token:token`.
