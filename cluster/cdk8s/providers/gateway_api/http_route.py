"""Ergonomic wrapper for Gateway API's `HTTPRoute`, following cdk8s-plus's own construction
pattern: a class named after the kind, constructed as `HttpRoute(scope, id, ...)`, and named
`@staticmethod` factories grouping a rule fragment's real variant shapes under one type, each
returning the generated struct. No shared-Gateway, hostname, or backend fact lives here -- those
are this cluster's own values, built in `cluster.cdk8s.gateway`.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute as _HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecParentRefs,
    HttpRouteSpecRules,
    HttpRouteSpecRulesFilters,
    HttpRouteSpecRulesFiltersRequestRedirect,
    HttpRouteSpecRulesFiltersRequestRedirectPath,
    HttpRouteSpecRulesFiltersRequestRedirectScheme,
    HttpRouteSpecRulesFiltersRequestRedirectStatusCode,
    HttpRouteSpecRulesFiltersResponseHeaderModifier,
    HttpRouteSpecRulesFiltersResponseHeaderModifierAdd,
    HttpRouteSpecRulesFiltersResponseHeaderModifierSet,
    HttpRouteSpecRulesFiltersType,
    HttpRouteSpecRulesMatches,
    HttpRouteSpecRulesMatchesPath,
    HttpRouteSpecRulesMatchesPathType,
    HttpRouteSpecUseDefaultGateways,
)


class RouteMatch:
    """One `HTTPRouteMatch`. Gateway API discriminates three real path-match variants
    (`Exact`, `PathPrefix`, `RegularExpression`); only the two this repo builds today are
    wrapped -- add another factory the day a third one is needed.
    """

    @staticmethod
    def path_exact(value: str) -> HttpRouteSpecRulesMatches:
        return HttpRouteSpecRulesMatches(
            path=HttpRouteSpecRulesMatchesPath(type=HttpRouteSpecRulesMatchesPathType.EXACT, value=value)
        )

    @staticmethod
    def path_prefix(value: str) -> HttpRouteSpecRulesMatches:
        return HttpRouteSpecRulesMatches(
            path=HttpRouteSpecRulesMatchesPath(type=HttpRouteSpecRulesMatchesPathType.PATH_PREFIX, value=value)
        )


class RouteFilter:
    """One `HTTPRouteFilter`. Gateway API discriminates seven filter types
    (`RequestHeaderModifier`, `ResponseHeaderModifier`, `RequestMirror`,
    `RequestRedirect`, `URLRewrite`, `ExtensionRef`, `CORS`); only the two this repo
    builds today are wrapped -- add another the day a second one is needed.
    """

    @staticmethod
    def response_header_modifier(
        *,
        add: Sequence[HttpRouteSpecRulesFiltersResponseHeaderModifierAdd] | None = None,
        remove: Sequence[str] | None = None,
        set: Sequence[HttpRouteSpecRulesFiltersResponseHeaderModifierSet] | None = None,
    ) -> HttpRouteSpecRulesFilters:
        return HttpRouteSpecRulesFilters(
            type=HttpRouteSpecRulesFiltersType.RESPONSE_HEADER_MODIFIER,
            response_header_modifier=HttpRouteSpecRulesFiltersResponseHeaderModifier(
                add=list(add) if add else None, remove=list(remove) if remove else None, set=list(set) if set else None
            ),
        )

    @staticmethod
    def request_redirect(
        *,
        scheme: HttpRouteSpecRulesFiltersRequestRedirectScheme | None = None,
        status_code: HttpRouteSpecRulesFiltersRequestRedirectStatusCode | None = None,
        hostname: str | None = None,
        path: HttpRouteSpecRulesFiltersRequestRedirectPath | None = None,
        port: int | None = None,
    ) -> HttpRouteSpecRulesFilters:
        return HttpRouteSpecRulesFilters(
            type=HttpRouteSpecRulesFiltersType.REQUEST_REDIRECT,
            request_redirect=HttpRouteSpecRulesFiltersRequestRedirect(
                scheme=scheme, status_code=status_code, hostname=hostname, path=path, port=port
            ),
        )


class HttpRoute(_HttpRoute):
    """Gateway API's `HTTPRoute`. Every keyword is an `HTTPRouteSpec` field under its own name and
    type; `None` leaves it unset, so the CRD's own default applies."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        parent_refs: Sequence[HttpRouteSpecParentRefs] | None = None,
        hostnames: Sequence[str] | None = None,
        rules: Sequence[HttpRouteSpecRules] | None = None,
        use_default_gateways: HttpRouteSpecUseDefaultGateways | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=HttpRouteSpec(
                parent_refs=list(parent_refs) if parent_refs is not None else None,
                hostnames=list(hostnames) if hostnames is not None else None,
                rules=list(rules) if rules is not None else None,
                use_default_gateways=use_default_gateways,
            ),
        )
