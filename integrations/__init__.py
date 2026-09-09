"""Optional third-party integrations.

Nothing in this package is required to run AIOps Copilot. Each subpackage
targets one external system and is loaded only when explicitly enabled, so the
core agent stays independent of any particular vendor or in-house platform.

See ``integrations/windos/`` for a worked example of the integration contract:
register tool metadata with the registry, expose read-only MCP tools, and stay
inert when the target system is not configured.
"""
