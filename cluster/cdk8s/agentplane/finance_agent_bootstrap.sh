#!/bin/sh
marker=/state/workspaces/.agentplane-finance-agent-ready
mkdir -p /state/workspaces
if [ ! -f "$marker" ]; then
  git clone --branch main --single-branch http://finance-agent:agentplane-credential-forgejo-finance-agent@forgejo-http.forgejo.svc.cluster.local:3000/finance-agent/finance-agent.git /state/workspaces/finance-agent
  printf '%s\n' 'finance-agent workspace initialized' >"$marker"
fi
