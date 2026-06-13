# Ember ontology

- **Ember** – the agent's persona embodied by the LLM. Ember is the conversational
  agent whose reasoning and tool calls drive the system.
- **emberd** – lightweight Python agent runner hosting the LLM agent loop.
  emberd boots inside a container, wires up secrets/configuration, and repeatedly
  polls Matrix, samples the LLM from OpenAI API and executes tool calls from the LLM.
  The process lives under `/opt/emberd`.
- **LLM core** – the OpenAI Responses API model. Every cycle, emberd sends
  the current context to the LLM, receives reasoning + tool calls, and acts
  on them. LLM itself has no direct I/O; everything must flow through tools.
- **State directory** – `$EMBER_STATE_DIR` (typically `/var/lib/ember`).
  Stores conversation history.
- **Workspace** – `${EMBER_WORKSPACE_DIR:-/var/lib/ember/workspace}` is a
  persistent scratch area mounted from the same PVC. Ember drops temporary
  scripts, notes and other artifacts there.
- **Secrets surface** – `/var/run/ember/secrets/`, where Kubernetes projects the
  Matrix, Gitea, and OpenAI credentials. emberd monitors this directory so tokens can
  rotate without pod restarts.
- **Matrix runtime** – the runtime bridges Matrix rooms into the tool loop. The
  LLM only sees messages that emberd forwards and must reply via shell tools.
  Room membership is sourced live from the homeserver on startup.
