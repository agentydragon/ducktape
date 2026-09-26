"""Ergonomic building blocks for Gateway API's `HTTPRoute` rules, following
cdk8s-plus's own construction pattern: named `@classmethod` factories grouping a spec
fragment's real variant shapes under one type. No shared-Gateway, hostname, or backend
fact lives here -- those are this cluster's own values, built in `cluster.cdk8s.gateway`.
"""

from __future__ import annotations

from collections.abc import Sequence

from gateway_api_crds.io.k8s.networking.gateway import (
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
)


class RouteMatch:
    """One `HTTPRouteMatch`. Gateway API discriminates three real path-match variants
    (`Exact`, `PathPrefix`, `RegularExpression`); only exact-path matching is wrapped
    here -- add another factory the day a second one is needed.
    """

    def __init__(self, spec: HttpRouteSpecRulesMatches) -> None:
        self._spec = spec

    def to_spec(self) -> HttpRouteSpecRulesMatches:
        return self._spec

    @classmethod
    def path_exact(cls, value: str) -> RouteMatch:
        return cls(
            HttpRouteSpecRulesMatches(
                path=HttpRouteSpecRulesMatchesPath(type=HttpRouteSpecRulesMatchesPathType.EXACT, value=value)
            )
        )


class RouteFilter:
    """One `HTTPRouteFilter`. Gateway API discriminates seven filter types
    (`RequestHeaderModifier`, `ResponseHeaderModifier`, `RequestMirror`,
    `RequestRedirect`, `URLRewrite`, `ExtensionRef`, `CORS`); only the two this repo
    builds today are wrapped -- add another the day a second one is needed.
    """

    def __init__(self, spec: HttpRouteSpecRulesFilters) -> None:
        self._spec = spec

    def to_spec(self) -> HttpRouteSpecRulesFilters:
        return self._spec

    @classmethod
    def response_header_modifier(
        cls,
        *,
        add: Sequence[HttpRouteSpecRulesFiltersResponseHeaderModifierAdd] | None = None,
        remove: Sequence[str] | None = None,
        set: Sequence[HttpRouteSpecRulesFiltersResponseHeaderModifierSet] | None = None,
    ) -> RouteFilter:
        return cls(
            HttpRouteSpecRulesFilters(
                type=HttpRouteSpecRulesFiltersType.RESPONSE_HEADER_MODIFIER,
                response_header_modifier=HttpRouteSpecRulesFiltersResponseHeaderModifier(
                    add=list(add) if add else None,
                    remove=list(remove) if remove else None,
                    set=list(set) if set else None,
                ),
            )
        )

    @classmethod
    def request_redirect(
        cls,
        *,
        scheme: HttpRouteSpecRulesFiltersRequestRedirectScheme | None = None,
        status_code: HttpRouteSpecRulesFiltersRequestRedirectStatusCode | None = None,
        hostname: str | None = None,
        path: HttpRouteSpecRulesFiltersRequestRedirectPath | None = None,
        port: int | None = None,
    ) -> RouteFilter:
        return cls(
            HttpRouteSpecRulesFilters(
                type=HttpRouteSpecRulesFiltersType.REQUEST_REDIRECT,
                request_redirect=HttpRouteSpecRulesFiltersRequestRedirect(
                    scheme=scheme, status_code=status_code, hostname=hostname, path=path, port=port
                ),
            )
        )
