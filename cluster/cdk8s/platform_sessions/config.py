"""Non-secret platform-sessions integration and route policy."""

from typing import NotRequired, TypedDict

# Record all provider upstream exchanges through at least January 2027 to soak
# session renewal and rotation. Captures include credentials and private data.
RECORD_UPSTREAM_TRAFFIC = True


class BearerConfig(TypedDict):
    role: str
    token_file: str
    allow: NotRequired[list[dict[str, str]]]


THRIVE_READ_PATHS = (
    "/products",
    "/account/orders",
    "/account",
    "/carts",
    "/autoship/items",
    "/autoship/schedule/first",
)

BLUE_SHIELD_GATEWAY = "/memberwebapp/reverseproxy-secured/v10/bsc/aip/api/bsc/gateway/"
BLUE_SHIELD_READ_PATHS = (
    "member/message/center/read/v2",
    "es/documents/download",
    "member/coverages/pcp/goi/delegation/rules/preference/v4",
    "member/benefits/claims/accumulation/v2",
    "facets/claims/summary/v4",
    "facets/claims/details/v4",
    "member/claims/online/fetch/all/v1",
    "product/benefits/mhprovider/v5",
    "member/idcard/download/v4",
    "member/mhr/lookup/v2",
    "member/mhr/visits/v2",
    "member/mhr/medication/v2",
    "member/mhr/allergies/v2",
    "member/mhr/immunization/v2",
    "member/mhr/conditions/v2",
    "member/mhr/labresults/v2",
)

INTEGRATIONS: dict[str, list[dict[str, str | int]]] = {
    "integrations": [
        {"id": "openai", "type": "openai", "state_dir": "/data/session/openai"},
        {"id": "anthropic", "type": "anthropic", "state_dir": "/data/session/anthropic"},
        {"id": "chatgpt", "type": "chatgpt", "state_dir": "/data/session/chatgpt"},
        {
            "id": "blueshield",
            "type": "blueshield",
            "state_dir": "/data/session/blueshield",
            "poll_interval_seconds": 300,
        },
        {
            "id": "quest",
            "type": "quest",
            "state_dir": "/data/session/quest-platform",
            "poll_interval_seconds": 300,
        },
        {"id": "thrive", "type": "thrive", "state_dir": "/data/session/thrive"},
    ]
}

BEARERS: dict[str, BearerConfig] = {
    "owner": {"role": "owner", "token_file": "/var/run/platform-sessions/owner"},
    "blueshield-read": {
        "role": "routes",
        "token_file": "/var/run/platform-sessions/blueshield-read",
        "allow": [
            {"instance": "blueshield", "method": "GET", "path": BLUE_SHIELD_GATEWAY + "retrieve/globalprofile/info/v2"},
            *[
                {"instance": "blueshield", "method": "POST", "path": BLUE_SHIELD_GATEWAY + path}
                for path in BLUE_SHIELD_READ_PATHS
            ],
        ],
    },
    "chatgpt-read": {
        "role": "routes",
        "token_file": "/var/run/platform-sessions/chatgpt-read",
        "allow": [
            {"instance": "chatgpt", "method": "GET", "path": "/backend-api/conversations"},
            {"instance": "chatgpt", "method": "GET", "path": "/backend-api/conversations/*"},
            {"instance": "chatgpt", "method": "GET", "path": "/backend-api/conversations/*/messages"},
        ],
    },
    "thrive-scraper": {
        "role": "routes",
        "token_file": "/var/run/platform-sessions/thrive-scraper",
        "allow": [{"instance": "thrive", "method": "GET", "path": path} for path in THRIVE_READ_PATHS],
    },
    "thrive-read": {
        "role": "routes",
        "token_file": "/var/run/platform-sessions/thrive-read",
        "allow": [{"instance": "thrive", "method": "GET", "path": path} for path in THRIVE_READ_PATHS],
    },
    "aiquota": {
        "role": "routes",
        "token_file": "/var/run/platform-sessions/aiquota",
        "allow": [
            {"instance": "openai", "method": "GET", "path": "/v1/dashboard/billing/credit_grants"},
            {"instance": "anthropic", "method": "GET", "path": "/api/organizations"},
            {"instance": "anthropic", "method": "GET", "path": "/api/organizations/*/prepaid/credits"},
        ],
    },
    "quest-agent": {
        "role": "routes",
        "token_file": "/var/run/platform-sessions/quest",
        "allow": [
            {"instance": "quest", "method": "GET", "path": "/guest/getReasons/*"},
            {"instance": "quest", "method": "GET", "path": "/guest/getPscDetails/*"},
            {"instance": "quest", "method": "GET", "path": "/api/getInsuranceProfile"},
            {"instance": "quest", "method": "GET", "path": "/guest/getTokenBasicInfoAPI/*"},
            {"instance": "quest", "method": "GET", "path": "/guest/getEncounterInfo/*"},
            {"instance": "quest", "method": "GET", "path": "/guest/getPayorList/*"},
            {"instance": "quest", "method": "GET", "path": "/guest/getAppointment/*"},
            {"instance": "quest", "method": "POST", "path": "/guest/getPscsWithAvailability"},
            {"instance": "quest", "method": "POST", "path": "/api/getOpenEorders"},
            {"instance": "quest", "method": "POST", "path": "/api/getUserDemographics"},
            {"instance": "quest", "method": "POST", "path": "/guest/examoneEligibility"},
            {"instance": "quest", "method": "POST", "path": "/guest/verifyInsurance/V2"},
            {"instance": "quest", "method": "POST", "path": "/guest/getPricing"},
        ],
    },
}
