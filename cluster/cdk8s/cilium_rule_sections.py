"""Which Cilium policy rules Cilium refuses to import, shared by the fleet rules and
`cluster/validation`'s check of hand-written policies. Dependency-free so the latter can
import it without loading cdk8s.
"""

from collections.abc import Mapping
from typing import Any

_RULE_SECTIONS = ("ingress", "ingressDeny", "egress", "egressDeny")


def enforces_nothing(rule: Mapping[str, Any]) -> bool:
    """Whether a policy rule (`spec`, or an element of `specs`) has every rule section empty.

    Mirrors Cilium's `Rule.Sanitize`: such a rule is schema-valid but rejected at import
    ("rule must have at least one of Ingress, IngressDeny, Egress, EgressDeny"), leaving the
    policy `Valid=False` and silently unenforced -- a fail-open shape that widens exposure
    instead of breaking traffic (#4923). Default-deny is spelled as a single empty rule element
    (`ingress: [{}]`), which allows nothing while putting selected endpoints into default deny.
    """
    return not any(rule.get(section) for section in _RULE_SECTIONS)
