from __future__ import annotations

from importlib import resources

from mako.template import Template

from mako_utils.preprocessor import markdown_heading_preprocessor


def load_system_prompt() -> str:
    template_text = _read_resource("system_prompt.md.mako")
    template = Template(template_text, strict_undefined=True, preprocessor=markdown_heading_preprocessor)
    result: str = template.render(embed_package_file=_embed_package_file)
    return result


def _embed_package_file(relative_path: str) -> str:
    content = _read_resource(f"resources/{relative_path}")
    header = f"# /var/emberd/{relative_path}"
    return "\n".join((header, content))


def _read_resource(name: str) -> str:
    resource = resources.files("ember").joinpath(name)
    return resource.read_text(encoding="utf-8").rstrip()
