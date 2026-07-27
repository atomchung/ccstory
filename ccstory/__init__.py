"""ccstory — AI coding-agent usage recap with narrative.

Companion to ccusage: ccusage tells you the bill, ccstory tells you the story.
"""

def __getattr__(name: str):
    if name == "__version__":
        from .version import resolve_version

        version_val = resolve_version()
        globals()["__version__"] = version_val
        return version_val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
