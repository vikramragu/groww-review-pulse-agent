# Frontend

The dashboard: [`streamlit_app.py`](streamlit_app.py), a Streamlit app. Layout only.

| What | Where |
| --- | --- |
| The page (tabs, charts, buttons) | `frontend/streamlit_app.py` |
| Loading a run, re-running the checks, exporting/verifying a snapshot | `src/pulse/ui/data.py` (no Streamlit code; also used by `pulse snapshot`) |
| The public data | `snapshots/<week>.json` |
| Tests | `tests/test_ui.py` |

```bash
pip install -e ".[ui]"
streamlit run frontend/streamlit_app.py                         # local: your runs/
PULSE_UI_MODE=snapshot streamlit run frontend/streamlit_app.py  # preview the public view
```

On Streamlit Community Cloud, set the main file to `frontend/streamlit_app.py`.
Dependencies come from the root `requirements.txt`.
