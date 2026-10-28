from x.google_mcp_server.google_discovery import GenTool


def item_search_spec() -> GenTool:
    return GenTool(
        method_id="widgets.items.list",
        name="list_items",
        api_version="widgets.v1",
        expose=("query", "labels", "pageSize"),
    )
