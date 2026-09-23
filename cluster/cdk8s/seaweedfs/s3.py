"""SeaweedFS operator S3 objects as constructs: `Bucket`, `Identity` (or `IdentityRef` for
an IAM identity declared elsewhere) and the S3Credentials an identity mints, plus the
ResourceReferenceGrants that let another namespace's objects reference them.

Operator behaviour these encode (`cluster/skills/seaweed_operator/SKILL.md`):

- A Bucket's physical name is its `spec.name`; it is always the CR's own name.
- An `S3Identity` claims a cluster-global IAM name; it lives in the SeaweedFS namespace
  unless its tenant keeps it local.
- `S3Credentials` resolves `identityRef` literally when no same-namespace `S3Identity`
  exists, so it is named after the identity it holds a key for. It always retains its key
  and Secret: its CRD defaults to `Delete`.
- A Bucket, S3Identity or S3Credentials outside the SeaweedFS namespace may reference the
  `Seaweed` cluster only through a grant there. Each such construct adds its kind to its
  namespace's grant, which the chart holds once per namespace. A cross-namespace
  `secretRef` needs a grant in the Secret's namespace instead (`secret_grant`).
"""

from __future__ import annotations

from dataclasses import dataclass

from cdk8s import Chart, JsonPatch
from constructs import Construct
from seaweed_bucket_crds.com.seaweedfs.seaweed import (
    Bucket as BucketResource,  # The construct below takes the concept's name.
    BucketSpec,
    BucketSpecAccessActions,
    BucketSpecClusterRef,
    BucketSpecReclaimPolicy,
)
from seaweed_resourcereferencegrant_crds.com.seaweedfs.seaweed import (
    ResourceReferenceGrant,
    ResourceReferenceGrantSpec,
    ResourceReferenceGrantSpecFrom,
    ResourceReferenceGrantSpecTo,
)
from seaweed_s3credentials_crds.com.seaweedfs.seaweed import (
    S3Credentials,
    S3CredentialsSpec,
    S3CredentialsSpecIdentityRef,
    S3CredentialsSpecReclaimPolicy,
    S3CredentialsSpecSeaweedRef,
    S3CredentialsSpecSecretRef,
)
from seaweed_s3identity_crds.com.seaweedfs.seaweed import (
    S3Identity,
    S3IdentitySpec,
    S3IdentitySpecReclaimPolicy,
    S3IdentitySpecSeaweedRef,
)

from cluster.cdk8s.metadata import metadata

# Aliased: the constructs' own `namespace` parameter is the tenant's.
from cluster.cdk8s.seaweedfs import cluster, namespace as seaweedfs_namespace

_GROUP = "seaweed.seaweedfs.com"


@dataclass(frozen=True)
class SecretKeyFields:
    """The Secret keys S3Credentials writes the key pair to."""

    access_key: str
    secret_key: str


AWS_ENV_KEY_FIELDS = SecretKeyFields(access_key="AWS_ACCESS_KEY_ID", secret_key="AWS_SECRET_ACCESS_KEY")


def _description(description: str | None) -> dict[str, str] | None:
    return {"description": description} if description else None


def _seaweed_ref_namespace(namespace: str) -> str | None:
    """A reference to the Seaweed cluster names its namespace only from outside it."""
    return None if namespace == seaweedfs_namespace.NAME else seaweedfs_namespace.NAME


class _ClusterGrant(Construct):
    """The chart's one grant letting `namespace`'s objects reference the Seaweed cluster;
    `from` lists each kind once, in first-use order."""

    def __init__(self, chart: Chart, *, name: str, namespace: str, kind: str) -> None:
        super().__init__(chart, _ClusterGrant.id(namespace))
        self._namespace = namespace
        self._kinds = [kind]
        self._resource = ResourceReferenceGrant(
            self,
            "Resource",
            metadata=metadata(name, seaweedfs_namespace.NAME),
            spec=ResourceReferenceGrantSpec(
                from_=[ResourceReferenceGrantSpecFrom(group=_GROUP, kind=kind, namespace=namespace)],
                to=[ResourceReferenceGrantSpecTo(group=_GROUP, kind="Seaweed", name=cluster.NAME)],
            ),
        )

    @staticmethod
    def id(namespace: str) -> str:
        return f"seaweedfs-cluster-grant-{namespace}"

    def add(self, kind: str) -> None:
        if kind not in self._kinds:
            self._kinds.append(kind)
            self._resource.add_json_patch(
                JsonPatch.add("/spec/from/-", {"group": _GROUP, "kind": kind, "namespace": self._namespace})
            )


def _grant_cluster_reference(scope: Construct, *, namespace: str, kind: str, name: str) -> None:
    """Adds `kind` to `namespace`'s cluster grant, creating it named `name` on first use."""
    if namespace == seaweedfs_namespace.NAME:
        return
    chart = Chart.of(scope)
    grant = chart.node.try_find_child(_ClusterGrant.id(namespace))
    if grant is None:
        _ClusterGrant(chart, name=name, namespace=namespace, kind=kind)
    else:
        assert isinstance(grant, _ClusterGrant), grant
        grant.add(kind)


class IdentityRef(Construct):
    """An IAM identity this chart does not declare: another Kustomization's `Identity`, or
    one that predates the operator."""

    def __init__(self, scope: Construct, id: str, *, name: str) -> None:
        super().__init__(scope, id)
        self.name = name
        self._scope = scope

    def credentials(
        self,
        *,
        namespace: str,
        secret: str,
        key_fields: SecretKeyFields | None,
        secret_namespace: str | None = None,
        description: str | None = None,
    ) -> S3Credentials:
        """A key for this identity, mirrored into Secret `secret`. A same-namespace Secret is
        created and owned by the operator; one in `secret_namespace` must already exist and
        be granted (`secret_grant`). `key_fields=None` keeps `accessKey`/`secretKey`."""
        _grant_cluster_reference(self, namespace=namespace, kind="S3Credentials", name=self.name)
        # Beside this construct rather than under it, so objects render in call order.
        return S3Credentials(
            self._scope,
            f"{self.node.id}-credentials-{namespace}",
            metadata=metadata(self.name, namespace, annotations=_description(description)),
            spec=S3CredentialsSpec(
                seaweed_ref=S3CredentialsSpecSeaweedRef(name=cluster.NAME, namespace=_seaweed_ref_namespace(namespace)),
                identity_ref=S3CredentialsSpecIdentityRef(name=self.name),
                secret_ref=S3CredentialsSpecSecretRef(
                    name=secret,
                    namespace=secret_namespace,
                    access_key_field=key_fields.access_key if key_fields else None,
                    secret_key_field=key_fields.secret_key if key_fields else None,
                ),
                reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
            ),
        )


class Identity(IdentityRef):
    """Declares the S3Identity claiming IAM name `name`."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        namespace: str = seaweedfs_namespace.NAME,
        description: str | None = None,
    ) -> None:
        super().__init__(scope, id, name=name)
        S3Identity(
            self,
            "Resource",
            metadata=metadata(name, namespace, annotations=_description(description)),
            spec=S3IdentitySpec(
                seaweed_ref=S3IdentitySpecSeaweedRef(name=cluster.NAME, namespace=_seaweed_ref_namespace(namespace)),
                reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN,
            ),
        )
        _grant_cluster_reference(self, namespace=namespace, kind="S3Identity", name=name)


class Bucket(Construct):
    """A bucket of the Seaweed cluster, named `name` both as a CR and physically.

    `adopt_existing` takes over a physical bucket that already exists instead of failing
    with `BucketAlreadyExists`; `reclaim_policy=None` leaves the CRD default (`Retain`).
    `grant_name` names the namespace's cluster grant when this Bucket creates it (default:
    the bucket's name)."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        namespace: str,
        adopt_existing: bool,
        reclaim_policy: BucketSpecReclaimPolicy | None = BucketSpecReclaimPolicy.RETAIN,
        description: str | None = None,
        grant_name: str | None = None,
    ) -> None:
        super().__init__(scope, id)
        self._resource = BucketResource(
            self,
            "Resource",
            metadata=metadata(name, namespace, annotations=_description(description)),
            spec=BucketSpec(
                name=name,
                adopt_existing=adopt_existing or None,
                cluster_ref=BucketSpecClusterRef(name=cluster.NAME, namespace=seaweedfs_namespace.NAME),
                reclaim_policy=reclaim_policy,
            ),
        )
        self._has_access = False
        _grant_cluster_reference(self, namespace=namespace, kind="Bucket", name=grant_name or name)

    def grant(self, user: IdentityRef | str, *actions: BucketSpecAccessActions) -> None:
        """Adds a `spec.access` entry for an identity or a plain IAM user name (`anonymous`)."""
        entry = {"user": user.name if isinstance(user, IdentityRef) else user, "actions": list(actions)}
        self._resource.add_json_patch(
            JsonPatch.add("/spec/access/-", entry) if self._has_access else JsonPatch.add("/spec/access", [entry])
        )
        self._has_access = True

    def grant_read_write(self, user: IdentityRef | str) -> None:
        self.grant(
            user,
            BucketSpecAccessActions.READ,
            BucketSpecAccessActions.WRITE,
            BucketSpecAccessActions.LIST,
            BucketSpecAccessActions.TAGGING,
        )

    def grant_read(self, user: IdentityRef | str) -> None:
        self.grant(user, BucketSpecAccessActions.READ, BucketSpecAccessActions.LIST)


def secret_grant(scope: Construct, *, secret: str, namespace: str) -> ResourceReferenceGrant:
    """Permits S3Credentials in the SeaweedFS namespace to populate Secret `secret` in `namespace`."""
    return ResourceReferenceGrant(
        scope,
        f"secret-grant-{namespace}-{secret}",
        metadata=metadata(secret, namespace),
        spec=ResourceReferenceGrantSpec(
            from_=[
                ResourceReferenceGrantSpecFrom(group=_GROUP, kind="S3Credentials", namespace=seaweedfs_namespace.NAME)
            ],
            to=[ResourceReferenceGrantSpecTo(group="", kind="Secret", name=secret)],
        ),
    )
