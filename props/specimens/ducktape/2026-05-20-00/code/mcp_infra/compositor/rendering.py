from __future__ import annotations

from importlib import resources

from mako.template import Template

from mako_utils.preprocessor import markdown_heading_preprocessor
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.snapshots import ServerEntry


def render_compositor_instructions(states: dict[MCPMountPrefix, ServerEntry]) -> str:
    """Render grouped MCP server instructions/capabilities using a Mako template.

    Passes raw typed states directly to the template; filtering/sorting is done in the template.
    Returns an empty string if there are no running servers with content.

    Args:
        states: Dictionary of server prefixes to ServerEntry objects
    """
    if not states:
        return ""

    # Load template from package resources and render
    template_name = "compositor_instructions.md.mako"
    template_pkg = "mcp_infra.compositor.templates"
    template_text = resources.files(template_pkg).joinpath(template_name).read_text("utf-8")
    template = Template(template_text, preprocessor=markdown_heading_preprocessor)
    result: str = template.render(states=states, build_mcp_function=build_mcp_function)
    return result
