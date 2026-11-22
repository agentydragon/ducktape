"""The forgejo Namespace, written into the directory of the Kustomization that owns it."""

from __future__ import annotations

from functools import partial

from cluster.cdk8s.generation import namespace_chart
from cluster.cdk8s.namespaces import Vpa

NAMESPACE = "forgejo"

chart = partial(namespace_chart, name=NAMESPACE, vpa=Vpa.AUTO)
