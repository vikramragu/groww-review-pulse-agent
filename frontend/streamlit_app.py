"""Groww Review Pulse — dashboard.

    streamlit run frontend/streamlit_app.py

**Local** (finished runs in `runs/`, or a `.env`): browse any run, check health, and run
the week by hand -- *Fetch & analyse* (publishes nothing), read the note, then *Publish*
behind a confirmation, which also writes the public snapshot. Each action calls the same
`pulse` CLI, so the dashboard and the terminal can't disagree. There is no schedule: a
week runs when someone presses the button or types the command.

**Public** (Streamlit Community Cloud, no `runs/`): a read-only view of the latest
`snapshots/<week>.json`, verified on load. No token, no publishing.

Set `PULSE_UI_MODE=snapshot` to preview the public view locally.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from pulse import __version__
from pulse.config import load_env_file, load_settings
from pulse.models import Theme
from pulse.rendering.render import THEME_LABELS, figures_text
from pulse.ui.data import (
    UNCLASSIFIED,
    PulseView,
    SnapshotError,
    latest_snapshot,
    list_runs,
    load_run,
    load_snapshot,
)

#: The repository root: the app lives in frontend/, the runs and snapshots beside it.
ROOT = Path(__file__).resolve().parent.parent
RUNS = Path(os.environ.get("PULSE_UI_RUNS") or ROOT / "runs")
SNAPSHOTS = ROOT / "snapshots"

# Categorical slots in fixed theme order (validated: CVD-separated in light and dark).
# Colour follows the theme everywhere, never its rank.
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]
NEUTRAL = {"light": "#8a8984", "dark": "#7c7b76"}
SINGLE = {"light": "#2a78d6", "dark": "#3987e5"}

st.set_page_config(page_title="Groww Review Pulse", page_icon="📊", layout="wide")


def theme_mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except AttributeError:
        return "light"


def theme_colors() -> tuple[list[str], list[str]]:
    mode = theme_mode()
    series = SERIES_DARK if mode == "dark" else SERIES_LIGHT
    labels = [THEME_LABELS[t] for t in Theme] + ["Unclassified"]
    return labels, [*series, NEUTRAL[mode]]


def label_of(theme: str) -> str:
    return "Unclassified" if theme == UNCLASSIFIED else THEME_LABELS[Theme(theme)]


# --------------------------------------------------------------------------------------
# Source
# --------------------------------------------------------------------------------------


def choose_mode() -> str:
    forced = os.environ.get("PULSE_UI_MODE", "").strip().lower()
    if forced in {"local", "snapshot"}:
        return forced
    # A checkout with runs or a .env is someone's machine; Streamlit Cloud has neither.
    return "local" if list_runs(RUNS) or (ROOT / ".env").is_file() else "snapshot"


MODE = choose_mode()

if MODE == "local":
    # So the app sees what the CLI sees: the PULSE_DOC_ID override and the recipient.
    # Values are never displayed except the recipient address and the doc id.
    load_env_file(ROOT / ".env")


@st.cache_data(show_spinner=False)
def cached_run(path: str, stamp: float) -> PulseView:
    return load_run(Path(path))


@st.cache_data(show_spinner=False)
def cached_snapshot(path: str, stamp: float) -> PulseView:
    return load_snapshot(Path(path))


def load_view() -> PulseView | None:
    if MODE == "snapshot":
        path = latest_snapshot(SNAPSHOTS)
        if path is None:
            st.error("No snapshot found in `snapshots/`. Export one with `pulse snapshot`.")
            return None
        try:
            return cached_snapshot(str(path), path.stat().st_mtime)
        except SnapshotError as exc:
            st.error(f"The snapshot did not verify, so it is not shown: {exc}")
            return None

    runs = list_runs(RUNS)
    if not runs:
        return None
    names = [run.name for run in runs]
    chosen = st.sidebar.selectbox(
        "Run", names, help="Runs with a finished note, most recently updated first."
    )
    run_dir = RUNS / chosen
    return cached_run(str(run_dir), (run_dir / "run.json").stat().st_mtime)


# --------------------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------------------

st.title("Groww Review Pulse")
st.caption(f"v{__version__} · Google Play reviews only · weekly product-feedback pulse")

if MODE == "local":
    st.info(
        "**Local mode.** Publishing appends to the team's Google Doc and creates a Gmail "
        "**draft** through the MCP server. Nothing is ever sent.",
        icon=":material/info:",
    )

view = load_view()

if MODE == "snapshot" and view is not None:
    st.info(
        f"Read-only snapshot of the **{view.iso_week}** pulse (run {view.run_id}), exported "
        f"{view.exported_at} and re-verified on load. Review IDs are withheld; publishing "
        "is disabled.",
        icon=":material/lock:",
    )

if view is None and MODE == "local":
    st.warning("No finished run yet. Use **Fetch & analyse** in the sidebar.")


# --------------------------------------------------------------------------------------
# Local actions (sidebar)
# --------------------------------------------------------------------------------------


def run_cli(*args: str) -> tuple[int, str]:
    """Run the `pulse` CLI exactly as a user would, and return its exit code and output."""
    completed = subprocess.run(
        [sys.executable, "-m", "pulse", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=1800,
        env={**os.environ, "NO_COLOR": "1"},
    )
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def show_result(code: int, output: str, success: str) -> None:
    if code == 0:
        st.success(success)
    else:
        st.error(f"Exited with code {code}.")
    with st.expander("Output", expanded=code != 0):
        st.code(output[-8000:] or "(no output)", language="text")


ANALYSED = "Note ready. Read it, then publish it from the Delivery tab."


def analyse(week_args: list[str]) -> None:
    with st.spinner("Running stages 1-8…"):
        code, output = run_cli("run", "--dry-run", *week_args)
    if code != 0:
        show_result(code, output, "")
        return
    # Rerun so the new run is listed and selected; keep the output for after the rerun.
    st.session_state["last_result"] = (code, output)
    st.cache_data.clear()
    st.rerun()


if MODE == "local":
    with st.sidebar:
        if "last_result" in st.session_state:
            show_result(*st.session_state.pop("last_result"), ANALYSED)
        st.divider()
        st.subheader("Run this week")
        st.caption(
            "1. Fetch & analyse — publishes nothing.  \n"
            "2. Read the note and its checks.  \n"
            "3. Publish from the **Delivery** tab."
        )
        week = st.text_input("ISO week", placeholder="default: last complete week")
        week_args = ["--week", week.strip()] if week.strip() else []
        if st.button(
            "Fetch & analyse",
            type="primary",
            width="stretch",
            help="pulse fetch, then pulse run --dry-run (stages 1-8).",
        ):
            with st.spinner("Fetching public Play Store reviews…"):
                code, output = run_cli("fetch", *week_args)
            if code != 0:
                show_result(code, output, "")
            else:
                analyse(week_args)
        with st.expander("One step at a time"):
            if st.button("Fetch reviews", width="stretch", help="pulse fetch"):
                with st.spinner("Fetching public Play Store reviews…"):
                    show_result(*run_cli("fetch", *week_args), "Export written to data/raw/.")
            if st.button(
                "Analyse (dry run)",
                width="stretch",
                help="pulse run --dry-run: stages 1-8, writes the note, publishes nothing.",
            ):
                analyse(week_args)


# --------------------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------------------

if view is not None:
    tabs = st.tabs(
        [
            "Weekly pulse",
            "Themes, quotes & actions",
            "Review statistics",
            "Delivery",
            "Health & settings",
        ]
    )

    # -- Weekly pulse ------------------------------------------------------------------
    with tabs[0]:
        passed = sum(check.ok for check in view.checks)
        cols = st.columns(4)
        cols[0].metric("ISO week", view.iso_week)
        cols[1].metric(
            "Reviews analysed",
            view.week_review_count,
            help="Substantive reviews (English, more than 8 words) in the target week.",
        )
        cols[2].metric("Words", f"{view.word_count} / 250")
        cols[3].metric("Checks passed", f"{passed} / {len(view.checks)}")

        corpus = (
            f"{view.corpus_size:,} substantive of {view.corpus_total:,} reviews"
            if view.corpus_size and view.corpus_total
            else ""
        )
        st.caption(
            " · ".join(
                part
                for part in [
                    f"Window {view.window}" if view.window else "",
                    corpus,
                    f"run {view.run_id}",
                    f"baseline: prior {view.baseline_weeks} weeks",
                ]
                if part
            )
        )

        left, right = st.columns([3, 2], gap="large")
        with left:
            st.subheader("The note")
            with st.container(border=True):
                st.markdown(view.markdown)
        with right:
            st.subheader("Checks")
            for check in view.checks:
                st.markdown(f"{'✅' if check.ok else '❌'} **{check.name}** — {check.detail}")
            st.caption(
                "The same gate the pipeline runs before anything is published. "
                "A note that fails any check is never sent to the Doc or Gmail."
            )

    # -- Themes, quotes & actions ------------------------------------------------------
    with tabs[1]:
        st.subheader("All five themes this week")
        st.caption(
            "Ranked by how many reviews, how negative, and how fast a theme is rising, "
            "so the biggest theme is not always first. The top three go on the page."
        )
        table = pd.DataFrame(
            [
                {
                    "Rank": rank,
                    "Theme": row.label,
                    "On page": "✓" if row.on_page else "",
                    "Reviews": row.review_count,
                    "Share of week": row.share_of_week,
                    "Change vs prior weeks (pts)": row.delta_vs_baseline * 100,
                    "Mean ★": row.mean_rating,
                }
                for rank, row in enumerate(view.themes, start=1)
            ]
        )
        st.dataframe(
            table,
            hide_index=True,
            width="stretch",
            column_config={
                "Share of week": st.column_config.NumberColumn(format="percent"),
                "Change vs prior weeks (pts)": st.column_config.NumberColumn(format="%+.1f"),
                "Mean ★": st.column_config.NumberColumn(format="%.1f"),
            },
        )

        st.subheader("Top themes")
        for summary in view.note.top_themes:
            with st.container(border=True):
                st.markdown(f"**{THEME_LABELS[summary.theme]}** — {summary.summary}")
                st.caption(figures_text(summary))

        st.subheader("What users said")
        for quote in view.note.quotes:
            with st.container(border=True):
                st.markdown(f"> {quote.text}")
                st.caption(f"{quote.rating}★ · {THEME_LABELS[quote.theme]}")

        st.subheader("What to do next")
        for index, action in enumerate(view.note.actions, start=1):
            st.markdown(
                f"{index}. **{action.action}** ({THEME_LABELS[action.theme]}) — {action.rationale}"
            )

    # -- Review statistics -------------------------------------------------------------
    with tabs[2]:
        labels, colors = theme_colors()
        weekly = pd.DataFrame(view.weekly)
        if not weekly.empty:
            weekly["Theme"] = weekly["theme"].map(label_of)
            weekly["Share"] = weekly["share"]
            st.subheader("Theme share by week")
            st.caption(
                "Share of each week's substantive reviews. The last week is the one the "
                "note reports on."
            )
            hover = alt.selection_point(fields=["iso_week"], nearest=True, on="pointerover")
            base = alt.Chart(weekly).encode(
                x=alt.X("iso_week:N", title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("Share:Q", title=None, axis=alt.Axis(format="%", grid=True)),
                color=alt.Color(
                    "Theme:N",
                    scale=alt.Scale(domain=labels, range=colors),
                    legend=alt.Legend(orient="top", title=None),
                ),
            )
            lines = base.mark_line(strokeWidth=2)
            points = (
                base.mark_point(filled=True, size=64)
                .encode(
                    opacity=alt.condition(hover, alt.value(1), alt.value(0)),
                    tooltip=[
                        alt.Tooltip("iso_week:N", title="Week"),
                        alt.Tooltip("Theme:N"),
                        alt.Tooltip("count:Q", title="Reviews"),
                        alt.Tooltip("Share:Q", format=".1%"),
                    ],
                )
                .add_params(hover)
            )
            rule = (
                alt.Chart(weekly)
                .mark_rule(strokeWidth=1, opacity=0.4)
                .encode(x="iso_week:N")
                .transform_filter(hover)
            )
            st.altair_chart(lines + points + rule, width="stretch")
            with st.expander("Table view"):
                pivot = weekly.pivot_table(
                    index="iso_week", columns="Theme", values="count", aggfunc="sum"
                ).reindex(columns=labels)
                st.dataframe(pivot, width="stretch")

        single = SINGLE[theme_mode()]
        if view.ratings:
            st.subheader("Star ratings of substantive reviews")
            st.caption(
                "Long reviews skew negative: these describe what people complain about, "
                "not overall sentiment (77% of all raw reviews are 5★)."
            )
            rating_cols = st.columns(2)
            for col, key, title in (
                (rating_cols[0], "week", f"{view.iso_week}"),
                (rating_cols[1], "window", "Whole window"),
            ):
                frame = pd.DataFrame(
                    {
                        "Stars": [f"{s}★" for s in range(1, 6)],
                        "Reviews": [view.ratings[key].get(str(s), 0) for s in range(1, 6)],
                    }
                )
                chart = (
                    alt.Chart(frame, title=title)
                    .mark_bar(color=single, cornerRadiusEnd=4, size=28)
                    .encode(
                        x=alt.X("Stars:N", title=None, axis=alt.Axis(labelAngle=0)),
                        y=alt.Y("Reviews:Q", title=None),
                        tooltip=["Stars", "Reviews"],
                    )
                )
                col.altair_chart(chart, width="stretch")

        funnel = view.funnel
        if funnel.get("rows_exported"):
            st.subheader("From export to analysis")
            steps = [
                ("Rows exported", funnel.get("rows_exported")),
                ("In window, deduplicated", funnel.get("in_window")),
                ("Substantive (English, > 8 words)", funnel.get("substantive")),
            ]
            frame = pd.DataFrame(
                [{"Step": name, "Reviews": value} for name, value in steps if value]
            )
            chart = (
                alt.Chart(frame)
                .mark_bar(color=single, cornerRadiusEnd=4, size=24)
                .encode(
                    y=alt.Y("Step:N", sort=None, title=None),
                    x=alt.X("Reviews:Q", title=None),
                    tooltip=["Step", alt.Tooltip("Reviews:Q", format=",")],
                )
            )
            text = chart.mark_text(align="left", dx=6).encode(
                text=alt.Text("Reviews:Q", format=","),
                color=alt.value("currentColor"),
            )
            st.altair_chart(chart + text, width="stretch")
            dropped = funnel.get("dropped") or {}
            if dropped:
                st.caption(
                    "Removed as not substantive: "
                    + ", ".join(f"{reason} {count:,}" for reason, count in dropped.items())
                    + f". Reviews with personal details redacted: {funnel.get('pii_redacted')}."
                )

    # -- Delivery ----------------------------------------------------------------------
    with tabs[3]:
        delivery = view.delivery
        cols = st.columns(2)
        cols[0].metric("Google Doc", (delivery.get("doc_status") or "not published").title())
        cols[1].metric("Gmail draft", (delivery.get("draft_status") or "none").title())
        if delivery.get("document_url"):
            st.markdown(f"[Open the Google Doc]({delivery['document_url']})")
        elif delivery.get("doc_status"):
            st.caption("Published to the team's Google Doc (private to the team).")
        st.caption(
            "Delivery goes through the google-workspace-mcp server: the doc is appended to, "
            "the email is created as a **draft** for a person to read and send. "
            "The agent refuses to call any send tool."
        )

        if MODE == "local" and view.run_dir is not None:
            st.divider()
            st.subheader("Publish this run")
            settings = load_settings(ROOT / "config")
            doc_id = settings.pulse.docs.document_id or "(not configured)"
            target = "stub" if st.toggle("Use the in-repo stub server", value=False) else "live"
            st.markdown(
                f"Target: **{target}** server · doc `{doc_id}` · draft to "
                f"`{settings.recipient or 'PULSE_RECIPIENT is not set'}`"
            )
            st.caption(
                "If this week is already in that doc it is skipped, not duplicated. "
                "`PULSE_DOC_ID` in `.env` overrides the doc above."
            )
            confirmed = st.checkbox(f"I have read the {view.iso_week} note and want it published.")
            if st.button("Publish to Doc + create draft", type="primary", disabled=not confirmed):
                with st.spinner("Publishing through MCP…"):
                    code, output = run_cli(
                        "run",
                        "--mcp",
                        target,
                        "--from",
                        "9",
                        "--run-dir",
                        str(view.run_dir),
                        "--week",
                        view.iso_week,
                    )
                show_result(code, output, "Done. Check the Doc and your Gmail Drafts.")
                st.cache_data.clear()
                if code == 0 and target == "live":
                    # The public dashboard's copy. Writing it is local; pushing stays manual.
                    code, output = run_cli("snapshot", "--run-dir", str(view.run_dir))
                    written = f"Snapshot written to snapshots/{view.iso_week}.json."
                    show_result(code, output, written)
                    if code == 0:
                        st.markdown("To update the public dashboard, push it:")
                        st.code(
                            f'git add snapshots && git commit -m "snapshot: {view.iso_week}" '
                            "&& git push origin master:main",
                            language="bash",
                        )

    # -- Health & settings -------------------------------------------------------------
    with tabs[4]:
        usage = view.usage
        st.subheader("Model usage")
        if usage.get("calls"):
            cost = usage.get("estimated_cost_usd")
            cols = st.columns(3)
            cols[0].metric("Model calls", usage["calls"])
            cols[1].metric(
                "Tokens in / out", f"{usage['input_tokens']:,} / {usage['output_tokens']:,}"
            )
            cols[2].metric("Cost", f"${cost:.4f}" if cost is not None else "unknown")
            st.caption(f"Model: {usage.get('model')}")
            if not usage.get("complete", True):
                st.warning(
                    "This run's ledger predates per-stage accounting, so it records only the "
                    "drafting call; classification (≈43 calls) is not included.",
                    icon=":material/warning:",
                )
        else:
            st.caption("No model calls recorded for this run.")

        st.subheader("Theme taxonomy")
        st.caption("A closed set of five; reviews that fit none stay unclassified.")
        try:
            themes_cfg = load_settings(ROOT / "config").themes
            st.dataframe(
                pd.DataFrame(
                    [{"Theme": d.label, "Covers": d.covers} for d in themes_cfg.themes.values()]
                ),
                hide_index=True,
                width="stretch",
            )
        except Exception as exc:  # pragma: no cover - display only
            st.caption(f"Could not read config/themes.toml: {exc}")

        if MODE == "local":
            st.subheader("Health")
            if st.button("Run pulse doctor"):
                with st.spinner("Checking config, credentials and the MCP server…"):
                    code, output = run_cli("doctor")
                show_result(code, output, "All checks passed.")
