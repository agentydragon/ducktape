"""Non-secret platform-sessions integration and route policy."""

THRIVE_READ_PATHS = (
    "/products",
    "/account/orders",
    "/account",
    "/carts",
    "/autoship/items",
    "/autoship/schedule/first",
)

INTEGRATIONS = {
    "integrations": [
        {"id": "openai", "type": "openai", "state_dir": "/data/session/openai"},
        {"id": "anthropic", "type": "anthropic", "state_dir": "/data/session/anthropic"},
        {"id": "chatgpt", "type": "chatgpt", "state_dir": "/data/session/chatgpt"},
        {"id": "quest", "type": "quest", "state_dir": "/data/session/quest-platform"},
        {"id": "thrive", "type": "thrive", "state_dir": "/data/session/thrive"},
    ]
}

BEARERS = {
    "owner": {"role": "owner", "token_file": "/var/run/platform-sessions/owner"},
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
