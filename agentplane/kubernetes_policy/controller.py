"""Keeps the native RoleBindings and ClusterRoleBindings that PolicyBindings' Kubernetes rules expand to.

PolicySets and PolicyBindings in the policy namespace, and the ServiceAccounts in the subject
namespaces, are listed and watched into memory. Every change, and every binding's `expiresAt`,
triggers a pass that recomputes the whole expansion and converges the native bindings this
controller labels as its own: it creates the missing ones, deletes the rest, and applies each
object's `KubernetesReady` condition. A periodic pass repairs native bindings changed by hand, which
nothing watches. Nothing outside the label is ever touched, and another Agentplane instance's
controller labels its own with its own policy namespace.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import Callable, Set
from datetime import UTC, datetime
from functools import partial
from typing import Literal

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import CoreV1Api, RbacAuthorizationV1Api
from tenacity import AsyncRetrying, before_sleep_log, wait_exponential

from agentplane.crd_group import GROUP, VERSION
from agentplane.kubernetes_policy.expansion import Granted, NativeBinding, NativeKey, Refused, expand
from agentplane.policy.resources import (
    MESSAGE_LIMIT,
    POLICY_BINDINGS_PLURAL,
    POLICY_SETS_PLURAL,
    Condition,
    InvalidResource,
    ObjectMeta,
    PolicyBinding,
    PolicySet,
    RoleRef,
    parse_policy_binding,
    parse_policy_set,
)
from agentplane.subjects import ServiceAccountRef
from util.kubernetes import CustomObjectsClient
from util.kubernetes_watch import ListWatch, WatchedKind, apply_to

logger = logging.getLogger(__name__)

KUBERNETES_READY = "KubernetesReady"
FIELD_MANAGER = "agentplane-kubernetes-policy-controller"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
POLICY_NAMESPACE_LABEL = f"kubernetes-policy.{GROUP}/policy-namespace"
BINDING_ANNOTATION = f"kubernetes-policy.{GROUP}/binding"
BINDING_UID_ANNOTATION = f"kubernetes-policy.{GROUP}/binding-uid"
SUBJECT_UID_ANNOTATION = f"kubernetes-policy.{GROUP}/subject-uid"
_RBAC_GROUP = "rbac.authorization.k8s.io"
_APPLY_PATCH = "application/apply-patch+yaml"

type Native = k8s_client.V1RoleBinding | k8s_client.V1ClusterRoleBinding
# A KubernetesReady condition as (status, reason, message).
type _Desired = tuple[Literal["True", "False"], str, str]


class RoleMissingError(Exception):
    """Kubernetes accepts a binding to a role that does not exist; this controller does not create one."""


class _Roles:
    """Which referenced roles exist, read once per pass."""

    def __init__(self, rbac: RbacAuthorizationV1Api) -> None:
        self._rbac = rbac
        self._exists: dict[tuple[RoleRef, str | None], bool] = {}

    async def require(self, role: RoleRef, namespace: str | None) -> None:
        # A ClusterRole is one object whatever namespace binds it.
        key = (role, namespace if role.kind == "Role" else None)
        if key not in self._exists:
            try:
                if role.kind == "Role":
                    assert namespace is not None
                    await self._rbac.read_namespaced_role(role.name, namespace)
                else:
                    await self._rbac.read_cluster_role(role.name)
            except k8s_client.ApiException as error:
                if error.status != 404:
                    raise
                self._exists[key] = False
            else:
                self._exists[key] = True
        if not self._exists[key]:
            where = f" in {namespace}" if role.kind == "Role" else ""
            raise RoleMissingError(f"{role.kind} {role.name}{where} not found")


def _account_ref(obj: k8s_client.V1ServiceAccount) -> ServiceAccountRef:
    return ServiceAccountRef(namespace=obj.metadata.namespace, name=obj.metadata.name)


def _where(native: NativeBinding) -> str:
    return native.namespace or "cluster-wide"


class KubernetesPolicyController:
    def __init__(
        self,
        *,
        custom_objects: CustomObjectsClient,
        core_v1: CoreV1Api,
        rbac: RbacAuthorizationV1Api,
        policy_namespace: str,
        subject_namespaces: Set[str],
        resync_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._custom_objects = custom_objects
        self._rbac = rbac
        self._policy_namespace = policy_namespace
        self._subject_namespaces = subject_namespaces
        self._resync_seconds = resync_seconds
        self._clock = clock
        self._selector = f"{MANAGED_BY_LABEL}={FIELD_MANAGER},{POLICY_NAMESPACE_LABEL}={policy_namespace}"
        self._policy_sets: dict[str, PolicySet | InvalidResource] = {}
        self._bindings: dict[str, PolicyBinding | InvalidResource] = {}
        self._service_accounts: dict[ServiceAccountRef, str] = {}
        self._changed = asyncio.Event()
        self._watch = ListWatch(
            kinds=[
                WatchedKind(
                    name=POLICY_SETS_PLURAL,
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, policy_namespace, POLICY_SETS_PLURAL),
                    parse=parse_policy_set,
                    key=lambda obj: obj.metadata.name,
                    names=lambda: set(self._policy_sets),
                    apply=lambda key, obj: apply_to(self._policy_sets, key, obj),
                ),
                WatchedKind(
                    name=POLICY_BINDINGS_PLURAL,
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, policy_namespace, POLICY_BINDINGS_PLURAL),
                    parse=parse_policy_binding,
                    key=lambda obj: obj.metadata.name,
                    names=lambda: set(self._bindings),
                    apply=lambda key, obj: apply_to(self._bindings, key, obj),
                ),
                *(
                    WatchedKind(
                        name=f"{namespace}/serviceaccounts",
                        list=core_v1.list_namespaced_service_account,
                        args=(namespace,),
                        key=_account_ref,
                        names=partial(self._accounts_in, namespace),
                        apply=self._apply_service_account,
                    )
                    for namespace in sorted(subject_namespaces)
                ),
            ],
            resync_seconds=resync_seconds,
            on_change=self._on_change,
            on_cycle=self._on_cycle,
            clock=clock,
        )

    def _accounts_in(self, namespace: str) -> set[ServiceAccountRef]:
        return {ref for ref in self._service_accounts if ref.namespace == namespace}

    def _apply_service_account(self, key: ServiceAccountRef, obj: k8s_client.V1ServiceAccount | None) -> None:
        if obj is None:
            self._service_accounts.pop(key, None)
        else:
            self._service_accounts[key] = obj.metadata.uid

    async def _on_change(self, kind: WatchedKind) -> None:
        self._changed.set()

    async def _on_cycle(self, kind: WatchedKind, at: datetime) -> None:
        pass  # A pass runs on every change and every resync anyway.

    async def run(self) -> None:
        """Watch and reconcile until cancelled."""
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._watch.run(), name="kubernetes-policy-watch")
            tasks.create_task(self._reconcile_forever(), name="kubernetes-policy-reconcile")

    async def _reconcile_forever(self) -> None:
        next_expiry: datetime | None = None
        while True:
            timeout: float = self._resync_seconds
            if next_expiry is not None:
                timeout = min(timeout, max((next_expiry - self._clock()).total_seconds(), 0))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout)
            self._changed.clear()
            # Until every kind is listed, a missing binding is not a revoked one.
            if not self._watch.synced:
                continue
            async for attempt in AsyncRetrying(
                wait=wait_exponential(max=60), before_sleep=before_sleep_log(logger, logging.WARNING)
            ):
                with attempt:
                    next_expiry = await self.reconcile_once()

    async def reconcile_once(self) -> datetime | None:
        """One converging pass; returns the next instant a binding expires, when the next pass is due."""
        now = self._clock()
        # The watch keeps applying changes while this pass awaits; it works from one snapshot, and
        # the change that interrupted it starts the next pass.
        policy_sets, bindings = dict(self._policy_sets), dict(self._bindings)
        expansion = expand(
            policy_sets=policy_sets,
            bindings=bindings,
            service_accounts=dict(self._service_accounts),
            subject_namespaces=self._subject_namespaces,
            now=now,
        )
        actual = await self._list_natives()
        for key in actual.keys() - expansion.natives.keys():
            await self._delete(key)
        failures: defaultdict[str, list[str]] = defaultdict(list)
        roles = _Roles(self._rbac)
        for key, native in expansion.natives.items():
            try:
                await roles.require(native.role, native.namespace)
                existing = actual.get(key)
                if existing is not None and _matches(existing, native):
                    continue
                if existing is not None:
                    # Edited by hand; a roleRef cannot be changed in place.
                    await self._delete(key)
                await self._create(native)
            except RoleMissingError as error:
                failures[native.binding_name].append(f"{_where(native)}: {error}")
            except k8s_client.ApiException as error:
                logger.warning(
                    "binding %s %s for %s failed: %s", native.name, _where(native), native.binding_name, error
                )
                failures[native.binding_name].append(f"{_where(native)}: {error.status} {error.reason}")
        for policy_set in policy_sets.values():
            await self._apply_condition(POLICY_SETS_PLURAL, "PolicySet", policy_set, _set_condition(policy_set), now)
        for name, binding in bindings.items():
            desired = _binding_condition(expansion.verdicts[name], failures.get(name, []))
            await self._apply_condition(POLICY_BINDINGS_PLURAL, "PolicyBinding", binding, desired, now)
        return expansion.next_expiry

    async def _list_natives(self) -> dict[NativeKey, Native]:
        role_bindings = await self._rbac.list_role_binding_for_all_namespaces(label_selector=self._selector)
        cluster_bindings = await self._rbac.list_cluster_role_binding(label_selector=self._selector)
        return {NativeKey(item.metadata.namespace, item.metadata.name): item for item in role_bindings.items} | {
            NativeKey(None, item.metadata.name): item for item in cluster_bindings.items
        }

    async def _delete(self, key: NativeKey) -> None:
        try:
            if key.namespace is None:
                await self._rbac.delete_cluster_role_binding(key.name)
            else:
                await self._rbac.delete_namespaced_role_binding(key.name, key.namespace)
        except k8s_client.ApiException as error:
            if error.status != 404:
                raise

    async def _create(self, native: NativeBinding) -> None:
        body = self._render(native)
        try:
            if isinstance(body, k8s_client.V1RoleBinding):
                assert native.namespace is not None
                await self._rbac.create_namespaced_role_binding(native.namespace, body)
            else:
                await self._rbac.create_cluster_role_binding(body)
        except k8s_client.ApiException as error:
            # Created since the list; the next pass checks it like any other.
            if error.status != 409:
                raise

    def _render(self, native: NativeBinding) -> Native:
        metadata = k8s_client.V1ObjectMeta(
            name=native.name,
            namespace=native.namespace,
            labels={MANAGED_BY_LABEL: FIELD_MANAGER, POLICY_NAMESPACE_LABEL: self._policy_namespace},
            annotations={
                BINDING_ANNOTATION: f"{self._policy_namespace}/{native.binding_name}",
                BINDING_UID_ANNOTATION: native.binding_uid,
                SUBJECT_UID_ANNOTATION: native.subject_uid,
            },
        )
        role_ref = k8s_client.V1RoleRef(api_group=_RBAC_GROUP, kind=native.role.kind, name=native.role.name)
        subjects = [
            k8s_client.RbacV1Subject(
                kind="ServiceAccount", name=native.subject.name, namespace=native.subject.namespace
            )
        ]
        if native.namespace is None:
            return k8s_client.V1ClusterRoleBinding(metadata=metadata, role_ref=role_ref, subjects=subjects)
        return k8s_client.V1RoleBinding(metadata=metadata, role_ref=role_ref, subjects=subjects)

    async def _apply_condition(
        self, plural: str, kind: str, obj: PolicySet | PolicyBinding | InvalidResource, desired: _Desired, now: datetime
    ) -> None:
        observed = obj.status.condition(KUBERNETES_READY)
        status, reason, message = desired
        if observed is not None and (
            observed.status,
            observed.reason,
            observed.message,
            observed.observed_generation,
        ) == (status, reason, message, obj.metadata.generation):
            return
        condition = Condition(
            type=KUBERNETES_READY,
            status=status,
            reason=reason,
            message=message,
            observed_generation=obj.metadata.generation,
            last_transition_time=observed.last_transition_time
            if observed is not None and observed.status == status
            else now,
        )
        await self._write_condition(plural, kind, obj.metadata, condition)

    async def _write_condition(self, plural: str, kind: str, metadata: ObjectMeta, condition: Condition) -> None:
        # Server-side apply under this controller's field manager: the condition list is keyed by
        # type, so the other readers' conditions on the same object are left as they are.
        body = {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": kind,
            "metadata": {"name": metadata.name, "namespace": metadata.namespace},
            "status": {"conditions": [condition.model_dump(mode="json", by_alias=True, exclude_none=True)]},
        }
        try:
            await self._custom_objects.patch_namespaced_custom_object_status(
                GROUP,
                VERSION,
                metadata.namespace,
                plural,
                metadata.name,
                body,
                field_manager=FIELD_MANAGER,
                force=True,
                _content_type=_APPLY_PATCH,
            )
        except k8s_client.ApiException as error:
            # Deleted or edited meanwhile; the watch event that follows starts another pass.
            logger.warning("%s condition on %s %s failed: %s", KUBERNETES_READY, kind, metadata.name, error.reason)


def _set_condition(policy_set: PolicySet | InvalidResource) -> _Desired:
    if isinstance(policy_set, InvalidResource):
        return "False", "Invalid", policy_set.message
    return "True", "Valid", "rules accepted"


def _binding_condition(verdict: Refused | Granted, failures: list[str]) -> _Desired:
    if isinstance(verdict, Refused):
        return "False", verdict.reason, verdict.message
    problems = [*failures]
    if verdict.unavailable_sets:
        problems.append(f"missing or invalid PolicySets: {', '.join(verdict.unavailable_sets)}")
    if problems:
        reason = "ApplyFailed" if failures else "PolicySetUnavailable"
        return "False", reason, "; ".join(problems)[:MESSAGE_LIMIT]
    return "True", "Applied", f"{len(verdict.natives)} native bindings"


def _matches(actual: Native, native: NativeBinding) -> bool:
    annotations = actual.metadata.annotations or {}
    subjects = actual.subjects or []
    return (
        isinstance(actual, k8s_client.V1ClusterRoleBinding) == (native.namespace is None)
        and (actual.role_ref.kind, actual.role_ref.name) == (native.role.kind, native.role.name)
        and [(subject.kind, subject.namespace, subject.name) for subject in subjects]
        == [("ServiceAccount", native.subject.namespace, native.subject.name)]
        and annotations.get(BINDING_UID_ANNOTATION) == native.binding_uid
        and annotations.get(SUBJECT_UID_ANNOTATION) == native.subject_uid
    )
