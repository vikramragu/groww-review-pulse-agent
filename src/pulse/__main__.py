"""`python -m pulse` -- the same CLI as the `pulse` script, for callers that hold an
interpreter rather than a PATH (the dashboard's action buttons)."""

from pulse.cli import entrypoint

entrypoint()
