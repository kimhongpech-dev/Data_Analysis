"""MIS data dashboard.

Run from the project root:

    python -m streamlit run dashboard/app.py
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
SUPPORTED_TYPES = ["csv", "xlsx", "xlsm", "json"]

st.set_page_config(
    page_title="MIS data dashboard",
    page_icon=":material/analytics:",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def _read_csv(source: str | Path | io.BytesIO) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            if hasattr(source, "seek"):
                source.seek(0)
            return pd.read_csv(source, encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError("Could not decode the CSV file") from last_error


def _detect_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Turn text columns that hold dates into real datetime columns."""
    date_pattern = (
        r"\d{2,4}[-/.]\d{1,2}"
        r"|[A-Za-z]{3,}\s+\d{1,2}"
        r"|\d{1,2}\s+[A-Za-z]{3,}"
    )
    df = df.copy()
    for column in df.columns:
        if not (
            pd.api.types.is_object_dtype(df[column])
            or pd.api.types.is_string_dtype(df[column])
        ):
            continue
        non_null = df[column].dropna()
        if non_null.empty:
            continue

        sample = non_null.astype(str).head(200)
        if sample.str.contains(date_pattern, regex=True).mean() < 0.8:
            continue

        parsed = pd.to_datetime(non_null.astype(str), errors="coerce", format="mixed")
        if parsed.notna().mean() >= 0.95:
            df[column] = pd.to_datetime(df[column], errors="coerce", format="mixed")
    return df


@st.cache_data(show_spinner=False)
def load_path(path_str: str, modified: float) -> pd.DataFrame:
    """Load a processed or raw file from disk (cached by path + mtime)."""
    path = Path(path_str)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = _read_csv(path)
    elif suffix in {".xlsx", ".xlsm"}:
        df = pd.read_excel(path, sheet_name=0)
    elif suffix == ".json":
        df = pd.read_json(path)
    else:
        raise ValueError(f"Unsupported file type: {path.suffix}")
    return _detect_dates(df)


@st.cache_data(show_spinner=False)
def load_upload(name: str, payload: bytes) -> pd.DataFrame:
    """Load an uploaded file held in memory."""
    buffer = io.BytesIO(payload)
    suffix = Path(name).suffix.lower()
    if suffix == ".csv":
        df = _read_csv(buffer)
    elif suffix in {".xlsx", ".xlsm"}:
        df = pd.read_excel(buffer, sheet_name=0)
    elif suffix == ".json":
        df = pd.read_json(buffer)
    else:
        raise ValueError(f"Unsupported file type: {suffix}")
    return _detect_dates(df)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def prettify(name: str) -> str:
    return str(name).replace("_", " ").strip().capitalize()


def column_roles(df: pd.DataFrame) -> tuple[list[str], list[str], list[str]]:
    numeric = [
        column
        for column in df.columns
        if pd.api.types.is_numeric_dtype(df[column])
        and not pd.api.types.is_bool_dtype(df[column])
    ]
    dates = [
        column
        for column in df.columns
        if pd.api.types.is_datetime64_any_dtype(df[column])
    ]
    categorical = [
        column for column in df.columns if column not in numeric and column not in dates
    ]
    return numeric, dates, categorical


def filter_sidebar(
    df: pd.DataFrame, dataset_key: str, numeric: list[str], dates: list[str]
) -> pd.DataFrame:
    """Render low-cardinality filters in the sidebar and apply them."""
    candidates = [
        column
        for column in df.columns
        if column not in numeric
        and column not in dates
        and 1 < df[column].nunique(dropna=True) <= 25
    ]

    with st.sidebar:
        st.subheader("Filters")
        if not candidates:
            st.caption("No categorical columns with 2-25 values to filter on.")
        for column in candidates[:6]:
            options = sorted(df[column].dropna().astype(str).unique())
            selected = st.multiselect(
                prettify(column),
                options,
                default=options,
                key=f"filter_{dataset_key}_{column}",
            )
            if len(selected) < len(options):
                df = df[df[column].astype(str).isin(selected)]
        st.caption("Tip: switch light/dark mode in the ⋮ menu → Settings → Theme.")
    return df


def time_series_counts(df: pd.DataFrame, date_column: str) -> pd.DataFrame:
    values = df[date_column].dropna()
    if values.empty:
        return pd.DataFrame(columns=["period", "rows"])

    span = values.max() - values.min()
    frequency = "MS" if span.days > 180 else "D"
    series = (
        df.dropna(subset=[date_column])
        .set_index(date_column)
        .resample(frequency)
        .size()
    )
    return series.reset_index(name="rows").rename(columns={date_column: "period"})


def top_categories(df: pd.DataFrame, column: str, limit: int = 10) -> pd.DataFrame:
    counts = (
        df[column]
        .dropna()
        .astype(str)
        .value_counts()
        .head(limit)
        .rename_axis("category")
        .reset_index(name="rows")
    )
    return counts


def top_categories_chart(data: pd.DataFrame, category_label: str) -> alt.Chart:
    """Horizontal bars with the most common value highlighted."""
    if data.empty:
        return alt.Chart(pd.DataFrame({"x": [], "y": []})).mark_point()

    top_value = data.iloc[0]["category"]
    return (
        alt.Chart(data)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            x=alt.X("rows:Q", title="Rows"),
            y=alt.Y("category:N", sort="-x", title=category_label),
            color=alt.condition(
                alt.datum.category == top_value,
                alt.value("#2563EB"),
                alt.value("#94A3B8"),
            ),
            tooltip=[
                alt.Tooltip("category:N", title=category_label),
                alt.Tooltip("rows:Q", title="Rows", format=","),
            ],
        )
        .properties(height=max(240, min(460, 34 * len(data) + 40)))
    )


SEVERITY_ORDER = ["Low (<5%)", "Moderate (5-20%)", "High (20%+)"]
SEVERITY_COLORS = ["#22C55E", "#F59E0B", "#EF4444"]


def severity_label(missing_pct: float) -> str:
    """Map a missing-value percentage to a traffic-light category."""
    if missing_pct < 5:
        return SEVERITY_ORDER[0]
    if missing_pct < 20:
        return SEVERITY_ORDER[1]
    return SEVERITY_ORDER[2]


def missing_values_chart(data: pd.DataFrame) -> alt.Chart:
    """Colour-code missing-value bars by severity."""
    if data.empty:
        return alt.Chart(pd.DataFrame({"x": [], "y": []})).mark_point()

    plot = data.copy()
    plot["severity"] = plot["missing_pct"].map(severity_label)
    return (
        alt.Chart(plot)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            x=alt.X(
                "missing_pct:Q",
                title="Missing values (%)",
                scale=alt.Scale(domain=[0, 100]),
            ),
            y=alt.Y("column:N", sort="-x", title="Column"),
            color=alt.Color(
                "severity:N",
                title="Severity",
                scale=alt.Scale(
                    domain=SEVERITY_ORDER,
                    range=SEVERITY_COLORS,
                ),
                sort=SEVERITY_ORDER,
            ),
            tooltip=[
                alt.Tooltip("column:N", title="Column"),
                alt.Tooltip("missing:Q", title="Missing cells", format=","),
                alt.Tooltip("missing_pct:Q", title="Missing %", format=".1f"),
            ],
        )
        .properties(height=max(240, min(420, 34 * len(plot) + 40)))
    )


def aggregate(
    df: pd.DataFrame,
    x_column: str,
    y_column: str | None,
    measure: str,
    color_column: str | None = None,
) -> pd.DataFrame:
    """Group the data for the explorer chart, optionally by a colour dimension."""
    needed = [x_column]
    if y_column:
        needed.append(y_column)
    if color_column:
        needed.append(color_column)
    work = df[needed].dropna(subset=[x_column]).copy()

    output_columns = ["category", "value"] + (["series"] if color_column else [])
    if work.empty:
        return pd.DataFrame(columns=output_columns)

    if pd.api.types.is_datetime64_any_dtype(work[x_column]):
        span = work[x_column].max() - work[x_column].min()
        frequency = "MS" if span.days > 180 else "D"
        keys: list[object] = [pd.Grouper(key=x_column, freq=frequency)]
        if color_column:
            keys.append(color_column)
        grouped = work.groupby(keys, dropna=False, observed=True)
    else:
        keys = [x_column] + ([color_column] if color_column else [])
        grouped = work.groupby(keys, dropna=False, observed=True)

    if measure == "Count":
        values = grouped.size()
    elif measure == "Sum":
        values = grouped[y_column].sum()
    else:
        values = grouped[y_column].mean()

    out = values.reset_index(name="value").rename(columns={x_column: "category"})
    if color_column:
        out = out.rename(columns={color_column: "series"})
        out["series"] = out["series"].fillna("Not provided").astype(str)

    is_date = pd.api.types.is_datetime64_any_dtype(out["category"])
    if not is_date:
        out["category"] = out["category"].astype(str)
        if out["category"].nunique() > 30:
            totals = out.groupby("category")["value"].sum()
            keep = set(totals.nlargest(30).index)
            out = out[out["category"].isin(keep)]
        out = out.sort_values("value", ascending=False)

    return out


def explorer_chart(
    data: pd.DataFrame,
    chart_type: str,
    x_title: str,
    metric_title: str,
    series_title: str | None = None,
) -> alt.Chart:
    if data.empty:
        return alt.Chart(pd.DataFrame({"x": [], "y": []})).mark_point()

    has_series = series_title is not None and "series" in data.columns
    is_date = pd.api.types.is_datetime64_any_dtype(data["category"])

    category_tooltip = (
        alt.Tooltip("category:T", title=x_title)
        if is_date
        else alt.Tooltip("category:N", title=x_title)
    )
    tooltip = [category_tooltip]
    if has_series:
        tooltip.append(alt.Tooltip("series:N", title=series_title))
    tooltip.append(alt.Tooltip("value:Q", title=metric_title, format=",.2f"))

    if chart_type == "Line":
        x_encoding = (
            alt.X("category:T", title=x_title)
            if is_date
            else alt.X("category:N", title=x_title)
        )
        encoding: dict[str, object] = {
            "x": x_encoding,
            "y": alt.Y("value:Q", title=metric_title),
            "tooltip": tooltip,
        }
        if has_series:
            encoding["color"] = alt.Color("series:N", title=series_title)
        return (
            alt.Chart(data)
            .mark_line(point=True)
            .encode(**encoding)
            .properties(height=380)
        )

    if is_date:
        y_encoding = alt.Y("value:Q", title=metric_title)
        if has_series:
            y_encoding = alt.Y("value:Q", title=metric_title, stack="zero")
        encoding = {
            "x": alt.X("category:T", title=x_title),
            "y": y_encoding,
            "tooltip": tooltip,
        }
        if has_series:
            encoding["color"] = alt.Color("series:N", title=series_title)
        return (
            alt.Chart(data)
            .mark_bar(cornerRadiusEnd=2)
            .encode(**encoding)
            .properties(height=380)
        )

    if has_series:
        encoding = {
            "x": alt.X("value:Q", title=metric_title, stack="zero"),
            "y": alt.Y(
                "category:N",
                title=x_title,
                sort=alt.EncodingSortField(
                    field="value", op="sum", order="descending"
                ),
            ),
            "color": alt.Color("series:N", title=series_title),
            "tooltip": tooltip,
        }
    else:
        encoding = {
            "x": alt.X("value:Q", title=metric_title),
            "y": alt.Y("category:N", sort="-x", title=x_title),
            "tooltip": tooltip,
        }

    category_count = data["category"].nunique()
    return (
        alt.Chart(data)
        .mark_bar(cornerRadiusEnd=2)
        .encode(**encoding)
        .properties(height=max(240, min(460, 34 * category_count + 40)))
    )


def quality_summary(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for column in df.columns:
        missing = int(df[column].isna().sum())
        rows.append(
            {
                "column": column,
                "type": str(df[column].dtype),
                "non_null": int(df[column].notna().sum()),
                "missing": missing,
                "missing_pct": (missing / len(df) * 100) if len(df) else 0.0,
                "unique": int(df[column].nunique(dropna=True)),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Sidebar: data source
# ---------------------------------------------------------------------------
with st.sidebar:
    st.subheader("Data source")
    uploaded = st.file_uploader(
        "Open a CSV, Excel, or JSON file",
        type=SUPPORTED_TYPES,
        help="Upload a raw file to preview it without running the cleaning script.",
    )

    processed_files = sorted(
        (
            path
            for path in PROCESSED_DIR.glob("*")
            if path.suffix.lower() in {".csv", ".xlsx", ".xlsm", ".json"}
        ),
        key=lambda path: path.stat().st_mtime,
    )

    selected_path: Path | None = None
    if uploaded is None:
        if processed_files:
            labels = {path.name: path for path in processed_files}
            selected_name = st.selectbox(
                "Cleaned dataset",
                list(labels),
                index=len(labels) - 1,
            )
            selected_path = labels[selected_name]
            modified = datetime.fromtimestamp(selected_path.stat().st_mtime)
            st.caption(f"Last modified {modified:%d %b %Y, %H:%M}")
        else:
            st.caption("No cleaned datasets found in `data/processed/`.")
    else:
        st.caption(f"Using uploaded file: **{uploaded.name}**")


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------
st.title("MIS data dashboard")
st.caption("Explore, filter, and check the quality of your cleaned dataset.")

if uploaded is None and selected_path is None:
    with st.container(border=True):
        st.subheader("Add your data to get started")
        st.markdown(
            "1. Put your original CSV, Excel, or JSON file in `data/raw/`.\n"
            "2. Run `python src/clean_data.py` to create a cleaned dataset.\n"
            "3. Refresh this page and explore the results."
        )
    st.code(
        "python src/clean_data.py\npython -m streamlit run dashboard/app.py",
        language="bash",
    )
    st.stop()


# ---------------------------------------------------------------------------
# Load & filter
# ---------------------------------------------------------------------------
try:
    if uploaded is not None:
        dataframe = load_upload(uploaded.name, uploaded.getvalue())
        dataset_key = uploaded.name
        source_label = f"Uploaded file · {uploaded.name}"
    else:
        assert selected_path is not None
        dataframe = load_path(str(selected_path), selected_path.stat().st_mtime)
        dataset_key = selected_path.stem
        source_label = f"`{selected_path.relative_to(PROJECT_ROOT)}`"
except Exception as exc:  # noqa: BLE001 - show a readable message in the UI
    st.error(f"Could not load the dataset: {exc}", title="Unable to open data")
    st.stop()

if dataframe.empty:
    st.warning("The selected dataset has no rows.", title="Empty dataset")
    st.stop()

numeric_columns, date_columns, _ = column_roles(dataframe)
filtered = filter_sidebar(dataframe, dataset_key, numeric_columns, date_columns)

st.caption(
    f"{source_label} · {len(filtered):,} of {len(dataframe):,} rows · "
    f"{len(filtered.columns):,} columns"
)

missing_share = filtered.isna().to_numpy().mean() * 100 if filtered.size else 0
with st.container(horizontal=True):
    st.metric("Rows", f"{len(filtered):,}", border=True)
    st.metric("Columns", f"{len(filtered.columns):,}", border=True)
    st.metric("Missing cells", f"{missing_share:.1f}%", border=True)
    st.metric("Numeric columns", f"{len(numeric_columns):,}", border=True)

if filtered.empty:
    st.warning(
        "No rows match the current filters. Widen the selection in the sidebar.",
        title="Nothing to show",
    )
    st.stop()

with st.sidebar:
    st.download_button(
        "Download filtered data",
        data=filtered.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"{dataset_key}_filtered.csv",
        mime="text/csv",
        icon=":material/download:",
    )


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
overview_tab, explore_tab, quality_tab = st.tabs(
    ["Overview", "Explore", "Data quality"]
)

with overview_tab:
    chart_left, chart_right = st.columns(2)

    with chart_left:
        with st.container(border=True):
            if date_columns:
                date_column = date_columns[0]
                trend = time_series_counts(filtered, date_column)
                st.subheader("Records over time")
                if trend.empty:
                    st.caption("No valid dates to chart.")
                else:
                    st.line_chart(
                        trend,
                        x="period",
                        y="rows",
                        alt=f"Number of records over time by {prettify(date_column)}",
                    )
            elif numeric_columns:
                numeric_column = numeric_columns[0]
                st.subheader(f"Distribution of {prettify(numeric_column)}")
                histogram = (
                    alt.Chart(filtered.dropna(subset=[numeric_column]))
                    .mark_bar(cornerRadiusEnd=3, color="#2563EB")
                    .encode(
                        x=alt.X(
                            f"{numeric_column}:Q",
                            bin=alt.Bin(maxbins=30),
                            title=prettify(numeric_column),
                        ),
                        y=alt.Y("count():Q", title="Rows"),
                    )
                    .properties(height=320)
                )
                st.altair_chart(
                    histogram,
                    alt=f"Distribution of {prettify(numeric_column)}",
                )
            else:
                st.subheader("Records over time")
                st.caption("Add a date or numeric column to see a chart.")

    with chart_right:
        with st.container(border=True):
            categorical_candidates = [
                column
                for column in filtered.columns
                if column not in numeric_columns
                and column not in date_columns
                and filtered[column].nunique(dropna=True) > 1
            ]
            if categorical_candidates:
                category_column = min(
                    categorical_candidates,
                    key=lambda column: filtered[column].nunique(dropna=True),
                )
                counts = top_categories(filtered, category_column)
                st.subheader(f"Top values in {prettify(category_column)}")
                st.altair_chart(
                    top_categories_chart(counts, prettify(category_column)),
                    alt=f"Row counts for the most common values in {prettify(category_column)}",
                )
                st.caption(
                    "Bar length shows the number of rows; the most common value is highlighted."
                )
            elif numeric_columns:
                numeric_column = numeric_columns[0]
                st.subheader(f"{prettify(numeric_column)} by row")
                scatter_data = (
                    filtered[[numeric_column]].dropna().reset_index(drop=True).copy()
                )
                scatter_data["row_number"] = range(1, len(scatter_data) + 1)
                st.scatter_chart(
                    scatter_data,
                    x="row_number",
                    y=numeric_column,
                    alt=f"{prettify(numeric_column)} across the dataset",
                )
            else:
                st.subheader("Category breakdown")
                st.caption("Add a categorical column to see a breakdown.")

    with st.container(border=True):
        st.subheader("Data preview")
        st.dataframe(
            filtered.head(200),
            hide_index=True,
            alt="First 200 rows of the filtered dataset",
        )
        if len(filtered) > 200:
            st.caption(f"Showing the first 200 of {len(filtered):,} rows.")

with explore_tab:
    if st.session_state.get("explore_dataset") != dataset_key:
        for key in (
            "explore_x",
            "explore_measure",
            "explore_chart",
            "explore_y",
            "explore_color",
        ):
            st.session_state.pop(key, None)
        st.session_state["explore_dataset"] = dataset_key

    ordered_columns = (
        [
            column
            for column in filtered.columns
            if column not in numeric_columns and column not in date_columns
        ]
        + date_columns
        + numeric_columns
    )

    with st.container(horizontal=True):
        x_column = st.selectbox("Group by", ordered_columns, key="explore_x")
        measure_options = ["Count"] + (["Sum", "Average"] if numeric_columns else [])
        measure = st.segmented_control(
            "Measure",
            measure_options,
            default="Count",
            required=True,
            key="explore_measure",
        )
        chart_type = st.segmented_control(
            "Chart",
            ["Bar", "Line"],
            default="Bar",
            required=True,
            key="explore_chart",
        )

    color_options = [
        column
        for column in filtered.columns
        if column not in numeric_columns
        and column not in date_columns
        and column != x_column
        and 1 < filtered[column].nunique(dropna=True) <= 12
    ]
    color_column = None
    if color_options:
        valid_colors = ["None"] + color_options
        if st.session_state.get("explore_color") not in valid_colors:
            st.session_state.pop("explore_color", None)
        color_by = st.selectbox(
            "Color by (optional)",
            valid_colors,
            key="explore_color",
            help="Split the chart into one colour per category.",
        )
        if color_by != "None":
            color_column = color_by

    y_column = None
    if measure in {"Sum", "Average"}:
        y_column = st.selectbox("Value column", numeric_columns, key="explore_y")

    metric_title = "Rows" if measure == "Count" else f"{measure} of {prettify(y_column)}"
    series_title = prettify(color_column) if color_column else None
    chart_data = aggregate(filtered, x_column, y_column, measure, color_column)
    if (
        chart_type == "Line"
        and not chart_data.empty
        and not pd.api.types.is_datetime64_any_dtype(chart_data["category"])
    ):
        sort_columns = ["category"] + (["series"] if color_column else [])
        chart_data = chart_data.sort_values(sort_columns)
    chart = explorer_chart(
        chart_data,
        chart_type,
        prettify(x_column),
        metric_title,
        series_title,
    )
    st.altair_chart(
        chart,
        alt=f"{metric_title} by {prettify(x_column)}",
    )
    if color_column:
        st.caption(
            f"Colour splits **{prettify(x_column)}** by **{prettify(color_column)}**."
        )

    with st.container(border=True):
        st.subheader("Filtered data")
        st.dataframe(
            filtered.head(500),
            hide_index=True,
            alt="First 500 rows of the filtered dataset",
        )
        st.caption(f"Showing the first 500 of {len(filtered):,} rows.")

with quality_tab:
    summary = quality_summary(filtered)

    overall_missing_pct = (
        filtered.isna().to_numpy().mean() * 100 if filtered.size else 0
    )
    if overall_missing_pct == 0:
        st.badge("Complete", color="green", icon=":material/check_circle:")
        st.caption("No missing cells in the current selection.")
    elif overall_missing_pct < 5:
        st.badge("Minor gaps", color="orange", icon=":material/info:")
        st.caption(
            f"{overall_missing_pct:.1f}% of cells are missing in the current selection."
        )
    else:
        st.badge("Needs attention", color="red", icon=":material/warning:")
        st.caption(
            f"{overall_missing_pct:.1f}% of cells are missing. Review the columns below."
        )

    with st.container(horizontal=True):
        st.metric("Duplicate rows", f"{int(filtered.duplicated().sum()):,}", border=True)
        st.metric(
            "Columns with missing values",
            f"{int((summary['missing'] > 0).sum()):,}",
            border=True,
        )
        st.metric(
            "Complete columns",
            f"{int((summary['missing'] == 0).sum()):,}",
            border=True,
        )

    with st.container(border=True):
        st.subheader("Missing values by column")
        missing_columns = (
            summary[summary["missing"] > 0]
            .sort_values("missing_pct", ascending=False)
            .head(15)
        )
        if missing_columns.empty:
            st.success("No missing values in the current selection.")
        else:
            st.altair_chart(
                missing_values_chart(missing_columns),
                alt=(
                    "Percentage of missing values by column, colour-coded "
                    "from green to red by severity"
                ),
            )
            st.caption("Green: under 5% missing · Amber: 5-20% · Red: 20% or more.")

    with st.container(border=True):
        st.subheader("Column summary")
        st.dataframe(
            summary,
            hide_index=True,
            column_config={
                "column": st.column_config.TextColumn("Column", pinned=True),
                "type": st.column_config.TextColumn("Type"),
                "non_null": st.column_config.NumberColumn("Non-null", format="%d"),
                "missing": st.column_config.NumberColumn("Missing", format="%d"),
                "missing_pct": st.column_config.ProgressColumn(
                    "Missing %", min_value=0, max_value=100, format="%.1f%%"
                ),
                "unique": st.column_config.NumberColumn("Unique", format="%d"),
            },
            alt="Completeness summary for every column",
        )
