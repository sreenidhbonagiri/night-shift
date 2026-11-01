"""Fixed read-only tools for the Night Shift Engineer agent.

These tools resolve ONLY direct-child files inside the fixture directory.
Path traversal, parent references, and subdirectory access are all rejected.
No file outside the fixture can be read. No write capability exists.
"""
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "test_app"


def _resolve_path(name: str) -> Path:
    """Resolve a requested file name to a direct child of the fixture dir.

    Rejects path separators, parent references, and absolute paths.
    Returns the resolved Path if valid, else None.
    """
    if not isinstance(name, str):
        return None
    name = name.strip()
    # Reject anything with path separators or parent references.
    if "/" in name or "\\" in name or name == ".." or name.startswith(".."):
        return None
    # Also reject hidden files and the current/parent directory entries.
    if name.startswith(".") or name in ("", "."):
        return None
    p = FIXTURE_DIR / name
    # Final safety: must be inside FIXTURE_DIR and must exist.
    if not p.is_file() or FIXTURE_DIR not in p.resolve().parents:
        return None
    return p


def list_files(**kwargs) -> dict[str, Any]:
    """List the direct files in the fixture directory.

    The fixture directory is flat; there are no subdirectories.
    A `directory` argument is rejected with a helpful message.
    """
    if kwargs.get("directory"):
        return {
            "error": (
                "The fixture directory has no subdirectories. "
                "Call list_files() with no arguments."
            )
        }
    files = sorted(p.name for p in FIXTURE_DIR.iterdir() if p.is_file())
    return {"files": files}


def read_file(path: str = "") -> dict[str, Any]:
    """Read a direct-child file from the fixture directory.

    Only bare file names are accepted (e.g. 'main.py').
    Path forms like 'app/main.py' are rejected with a hint to use the bare name.
    """
    if not isinstance(path, str) or not path.strip():
        return {"error": "Provide a file name (e.g. 'main.py')."}
    path = path.strip()
    if "/" in path or "\\" in path:
        return {
            "error": (
                f"Path forms like '{path}' are not supported. "
                "Use the bare file name (e.g. 'main.py')."
            )
        }
    resolved = _resolve_path(path)
    if resolved is None:
        return {"error": f"File '{path}' not found in the fixture directory."}
    try:
        content = resolved.read_text(encoding="utf-8")
    except Exception as exc:
        return {"error": f"Could not read '{path}': {exc}"}
    return {"file": resolved.name, "content": content}
