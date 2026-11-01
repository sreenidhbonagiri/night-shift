"""Regression tests for the isolated Night Shift test app."""
import pytest
from main import render_attrs, render_greeting


def test_render_greeting_escapes_html():
    malicious = "<script>alert('xss')</script>"
    output = render_greeting(malicious)
    assert "<script>" not in output
    assert "&lt;script&gt;" in output


def test_xmlattr_renders_normal_attributes():
    """Normal attribute keys render correctly with valid values."""
    output = render_attrs({"class": "ok", "data-id": "42"})
    assert 'class="ok"' in output
    assert 'data-id="42"' in output


def test_xmlattr_rejects_space_in_key():
    """CVE-2024-22195: keys with spaces must raise ValueError, not inject attributes."""
    with pytest.raises(ValueError):
        render_attrs({"class": "ok", "onclick alert": "x"})


def test_xmlattr_rejects_slash_in_key():
    """CVE-2024-34064: keys with '/' must raise ValueError, not inject attributes."""
    with pytest.raises(ValueError):
        render_attrs({"class": "ok", "onclick/x": "x"})
