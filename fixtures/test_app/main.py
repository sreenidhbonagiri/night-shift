"""Minimal synthetic app used by Night Shift QA regression tests.

Tests the dependency patching workflow, not business logic.
"""
from jinja2 import Environment, select_autoescape, PackageLoader


def render_attrs(user_keys: dict) -> str:
    """Render HTML attributes using Jinja2's xmlattr filter."""
    env = Environment(autoescape=select_autoescape(["html", "xml"]))
    template = env.from_string('<div{{ attrs|xmlattr }}></div>')
    return template.render(attrs=user_keys)


def render_greeting(name: str) -> str:
    env = Environment(autoescape=select_autoescape(["html", "xml"]))
    template = env.from_string("<p>Hello, {{ name }}!</p>")
    return template.render(name=name)


if __name__ == "__main__":
    print(render_greeting("Night Shift"))
