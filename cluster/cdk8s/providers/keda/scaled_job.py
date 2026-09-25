"""Named factories for KEDA `ScaledJob` trigger shapes the CRD schema itself leaves as an
untyped `type`/`metadata` pair, one per scaler this repo uses.
"""

from __future__ import annotations

from keda_scaledjob_crds.sh.keda import ScaledJobSpecTriggers, ScaledJobSpecTriggersAuthenticationRef

from cluster.cdk8s.providers.keda.trigger_authentication import TriggerAuthentication


def forgejo_runner_trigger(
    *, address: str, owner: str, repo: str, labels: str, authentication: TriggerAuthentication
) -> ScaledJobSpecTriggers:
    """A `triggers[]` entry for KEDA's `forgejo-runner` scaler, counting queued Actions jobs
    matching `labels` on the Forgejo instance at `address`: https://keda.sh/docs/2.20/scalers/forgejo/.

    No `name:` -- the docs list it as required, but the scaler filters on `labels` and works
    without one. A fixed name could not match anyway: every pod registers under its own pod
    name. Stays a function, not a factory on a class: the schema leaves `type`/`metadata`
    fully untyped, so there's no shared type to group scaler shapes under -- add another
    function the day a second scaler is used.
    """
    return ScaledJobSpecTriggers(
        type="forgejo-runner",
        metadata={"address": address, "owner": owner, "repo": repo, "labels": labels},
        authentication_ref=ScaledJobSpecTriggersAuthenticationRef(name=authentication.name),
    )
