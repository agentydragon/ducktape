#!/bin/sh
marker=/state/workspaces/.agentplane-haku-ready
mkdir -p /state/workspaces
if [ ! -f "$marker" ]; then
  git clone --depth 1 --branch main --single-branch http://haku:agentplane-credential-forgejo-haku@forgejo-http.forgejo.svc.cluster.local:3000/haku/haku-state.git /state/workspaces/haku-state
  git clone --depth 1 --branch devel --single-branch https://github.com/agentydragon/ducktape.git /state/workspaces/ducktape
  printf '%s\n' 'haku workspace initialized' >"$marker"
fi
