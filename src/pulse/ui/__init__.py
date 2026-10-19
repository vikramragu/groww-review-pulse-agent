"""The dashboard: a Streamlit view over runs (local) or a published snapshot (public).

`data` holds everything testable -- loading a run, exporting and verifying a snapshot --
and imports no Streamlit, so the dashboard adds no logic the CLI does not already have.
`streamlit_app.py` at the repository root only lays it out.
"""
