#!/bin/bash
set -euo pipefail

TOKEN=$(cat /var/run/secrets/sa-token/token)
GITHUB_PAT=$(cat /var/run/secrets/github-pat/token)

git clone --depth=1 --branch=devel \
  "https://x-access-token:${GITHUB_PAT}@github.com/agentydragon/ducktape.git" \
  /tmp/repo
cd /tmp/repo

cat >secrets/claude-web-k8s-token.yaml <<EOF
k8s_token: ${TOKEN}
EOF

sops encrypt --in-place secrets/claude-web-k8s-token.yaml

git config user.name "claude-token-rotation"
git config user.email "noreply@allegedly.works"
git add secrets/claude-web-k8s-token.yaml

if git diff --cached --quiet; then
  echo "No changes to commit"
else
  git commit -m "chore: rotate Claude web K8s token ($(date -I))"
  git push origin devel
fi
