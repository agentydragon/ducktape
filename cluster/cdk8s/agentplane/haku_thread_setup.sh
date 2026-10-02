#!/bin/sh
git clone --depth 1 --branch main --single-branch http://haku:agentplane-credential-forgejo-haku@forgejo-http.forgejo.svc.cluster.local:3000/haku/haku-state.git .
git clone --depth 1 --branch devel --single-branch https://github.com/agentydragon/ducktape.git ../ducktape
