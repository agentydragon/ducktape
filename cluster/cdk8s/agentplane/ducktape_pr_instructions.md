When contributing to the public `agentydragon/ducktape` repository, use the dedicated
`agentydragon-agent` GitHub identity, not the operator's identity. Check the current Agentplane
egress rules before GitHub access. The `github-agentydragon-agent` rule supplies a substituted PAT:
use its exact returned placeholder as an HTTP Basic password for Git push and as a Bearer token
for GitHub API calls through the proxy. Never read, log, or persist the real PAT.

Work in a ducktape checkout (not nested inside another repository). Fork to
`agentydragon-agent/ducktape` if the fork does not exist, push the change branch to that fork,
then open a PR from `agentydragon-agent:<branch>` against `agentydragon/ducktape`'s `devel`
branch with the substituted PAT. Never push directly to the upstream repository. Check the
diff for personal data before pushing; public ducktape must contain only generic tooling and
public-safe instructions.
