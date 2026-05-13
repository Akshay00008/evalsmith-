# lib/__init__.py
# The `lib` package is the implementation core. Slash commands, MCP servers,
# the CLI, and the tests all import from here. Nothing in `lib` should ever
# import from `.claude/`, `mcp_servers/`, or `tools/` — keep the dependency
# arrow pointing inward.

__version__ = "0.1.0"
# Framework version is stamped onto every artifact (Mission, TrialResult, etc.)
# so that `genai replay` knows which schema migrations to apply when reading
# old experiment logs.
FRAMEWORK_VERSION = __version__
