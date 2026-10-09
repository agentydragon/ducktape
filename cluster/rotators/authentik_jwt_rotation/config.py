"""The rotator's configuration schema: the roster `rotate.py --config` reads.

Kept apart from `rotate.py` so the cdk8s generator (cluster/cdk8s/authentik/jwt_rotation.py),
which builds the roster from these models, imports pydantic and nothing of the runtime.
"""

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

AUTH_BASE = "https://auth.allegedly.works"

CredentialMode = Literal["client_secret", "user_password"]


class K8sSecretOutput(BaseModel):
    """Optional second output: write the minted token as a k8s Secret manifest.

    Lets a rotation feed an in-cluster consumer (Flux decrypts + applies the
    Secret) without that consumer ever touching SOPS. The `path` lives under
    `cluster/k8s/` so the `.sops.yaml` cluster catch-all rule encrypts stringData
    for Flux + admin. The token's `exp` epoch is written alongside so consumers
    that need a monotonic "the token changed" signal (e.g. a Terraform write-only
    `*_wo_version`) have one without decrypting the token.

    Adding a `k8s_secret` is a TWO-file change and the rotator only writes one
    of them: it commits `path`, but nothing teaches any kustomization to
    reference it. Two constraints bite in opposite directions --

    - reference the path before the first mint and `kustomize build` fails for
      the whole namespace (missing resource), and
    - leave it unreferenced after the first mint and `test_no_orphaned_files`
      goes red on `devel`, since the CronJob commits straight to the shared
      branch.

    So there is no safe window to defer the wiring into: satisfy both in one
    change. Committing an encrypted seed placeholder at `path` alongside the
    resource line does that -- the file exists for `kustomize build`, is
    referenced from the start, and the first run overwrites it (a stale
    `expires_unencrypted` forces the mint). That is exactly the failure mode
    `probe` exists to catch: "a seed placeholder never overwritten".

    `haku-openclaw-spike-kube-token` is the worked example of getting
    this wrong -- #3772 deferred the resource line behind a comment asking a
    human to remember, the rotator landed the manifest 4.5 min later, and CI on
    `devel` stayed red ~4h48m until #3780 added the line. A comment promising a
    follow-up is not a mechanism.
    """

    path: Path = Field(description="Repo-relative path for the Secret manifest (under cluster/k8s/, *.sops.yaml)")
    name: str
    namespace: str
    token_key: str = Field(default="jwt", description="stringData key for the JWT")
    exp_key: str = Field(default="token-exp", description="stringData key for the JWT exp epoch (seconds)")


class Probe(BaseModel):
    """Live check that the *published* token still works against its real endpoint.

    The job holds no age key, so the sops file cannot be read back; the probe
    instead reads the in-cluster Secret the `k8s_secret` output feeds and sends
    its token as a Bearer to `url`. Only an explicit 401/403 is a credential
    verdict — anything else means the request made it past auth (or the endpoint
    is sick, which is not the token's fault).
    """

    url: str = Field(description="Endpoint that answers 401/403 iff the bearer is bad")
    method: Literal["GET", "POST"] = "GET"


class Rotation(BaseModel):
    name: str = Field(description="Human-readable name for logs and the commit message")
    provider_slug: str = Field(description="Authentik provider slug; pins the expected source-JWT issuer")
    scopes: str = Field(description="OAuth scopes for the client_credentials mint")
    credential_mode: CredentialMode = Field(
        default="client_secret",
        description="How to authenticate to the token endpoint: provider client_secret or service-account username/password",
    )
    credentials_dir: Path = Field(
        description="Mounted secret dir holding client credentials (+ proxy_client_id when exchanging)"
    )
    sops_file: Path = Field(description="Repo-relative path to the encrypted output file")
    token_field: str = Field(description="YAML field name under which the token is written")
    expected_group: str | None = Field(
        default=None, description="Group claim that must be present on the source JWT; aborts the rotation if missing"
    )
    expected_audiences: list[str] | None = Field(
        default=None,
        description="Audiences (aud claim) that must all be present on the final written token. "
        "Asserted on every mint, and forces a re-mint when the stored token's "
        "audiences_unencrypted stamp does not already cover them — so adding an audience "
        "rolls out on the next run instead of waiting for expiry.",
    )
    expected_claims: dict[str, str] | None = Field(
        default=None,
        description="Other claims (e.g. email) that must equal the given value on the final "
        "written token. Asserted on every mint, and forces a re-mint when the stored token's "
        "claims_unencrypted stamp does not already match — so fixing the upstream Authentik "
        "attribute (e.g. a service account's missing email) rolls out on the next run instead "
        "of waiting for expiry.",
    )
    exchange_scopes: str | None = Field(
        default=None,
        description="When set, exchange the source JWT into a proxy-provider token "
        "(client_id from credentials_dir/proxy_client_id) with these scopes",
    )
    k8s_secret: K8sSecretOutput | None = Field(
        default=None,
        description="When set, also write the minted token as a k8s Secret manifest (in addition "
        "to sops_file) for an in-cluster consumer to read via Flux.",
    )
    probe: Probe | None = Field(
        default=None,
        description="When set (requires k8s_secret), read the published Secret back each run and "
        "verify its token against the real endpoint; a 401/403 forces a re-mint.",
    )

    @model_validator(mode="after")
    def _probe_requires_k8s_secret(self) -> Self:
        if self.probe and not self.k8s_secret:
            raise ValueError(f"{self.name}: probe requires k8s_secret (the sops file cannot be read back)")
        return self

    @property
    def expected_issuer(self) -> str:
        return f"{AUTH_BASE}/application/o/{self.provider_slug}/"


class Config(BaseModel):
    rotations: list[Rotation]
    rotate_below_hours: int = Field(
        default=24, description="Mint a fresh token once remaining validity drops below this"
    )
    github_repo: str = "agentydragon/ducktape"
    sops_config: str = Field(default=".sops.yaml", description="Repo path sops reads to pick the recipient set")
    git_author_name: str = "authentik-jwt-rotation"
    git_author_email: str = "noreply@allegedly.works"
