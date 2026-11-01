"""Clean raw datasets for the MIS dashboard.

The script reads every supported file in ``data/raw`` (CSV, Excel, JSON),
tidies column names, removes empty rows/columns and duplicates, infers
numbers and dates, and writes a dashboard-ready CSV to ``data/processed``.

Usage
-----
    python src/clean_data.py
    python src/clean_data.py --input data/raw/survey.xlsx
    python src/clean_data.py --fill-missing

By default missing values are preserved so data-quality issues stay visible.
Pass ``--fill-missing`` to impute numeric columns with their median and text
columns with "Not provided".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".json"}
MISSING_TOKENS = {"", "-", "--", "?", "na", "n/a", "nan", "none", "null"}

DATE_PATTERN = (
    r"\d{2,4}[-/.]\d{1,2}"          # 2024-01-05, 05/01/2024
    r"|[A-Za-z]{3,}\s+\d{1,2}"      # Jan 5
    r"|\d{1,2}\s+[A-Za-z]{3,}"      # 5 Jan
)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def _read_csv(path: Path) -> pd.DataFrame:
    """Read a CSV, trying common encodings and auto-detecting the delimiter."""
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return pd.read_csv(path, encoding=encoding, sep=None, engine="python")
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ValueError(f"Could not decode {path.name}") from last_error


def _read_excel(path: Path) -> pd.DataFrame:
    """Read an Excel workbook, combining sheets only when they share a schema."""
    sheets = pd.read_excel(path, sheet_name=None)
    if not sheets:
        raise ValueError(f"{path.name} contains no sheets")

    first_name = next(iter(sheets))
    first = sheets[first_name]
    same_schema = all(
        list(sheet.columns) == list(first.columns) for sheet in sheets.values()
    )

    if len(sheets) > 1 and same_schema:
        frames = []
        for sheet_name, sheet in sheets.items():
            sheet = sheet.copy()
            sheet["source_sheet"] = sheet_name
            columns = ["source_sheet"] + [
                column for column in sheet.columns if column != "source_sheet"
            ]
            frames.append(sheet[columns])
        return pd.concat(frames, ignore_index=True)

    if len(sheets) > 1:
        print(
            f"  ! {path.name} has {len(sheets)} sheets with different columns; "
            f"using the first sheet ('{first_name}')."
        )
    return first


def _read_json(path: Path) -> pd.DataFrame:
    """Read JSON records or a JSON object, flattening nested keys."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return pd.json_normalize(data)


def read_raw_file(path: Path) -> pd.DataFrame:
    """Load one supported raw file into a DataFrame."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _read_csv(path)
    if suffix in {".xlsx", ".xlsm"}:
        return _read_excel(path)
    if suffix == ".json":
        return _read_json(path)
    raise ValueError(f"Unsupported file type: {path.suffix}")


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------
def snake_case(name: object) -> str:
    """Turn a column header into a tidy snake_case name."""
    cleaned = re.sub(r"[^\w\s]", " ", str(name).strip())
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_").lower()
    return cleaned or "column"


def normalise_columns(columns: pd.Index) -> list[str]:
    """Normalise headers and make duplicate names unique."""
    used: set[str] = set()
    result: list[str] = []
    for column in columns:
        base = snake_case(column)
        candidate = base
        suffix = 1
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        result.append(candidate)
    return result


def _is_text(series: pd.Series) -> bool:
    return pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)


def _downcast_integers(df: pd.DataFrame) -> pd.DataFrame:
    """Store whole-number float columns as integers when possible."""
    for column in df.columns:
        series = df[column]
        if not pd.api.types.is_float_dtype(series) or series.dropna().empty:
            continue
        if (series.dropna() % 1 == 0).all():
            if series.isna().any():
                df[column] = series.astype("Int64")
            else:
                df[column] = series.astype("int64")
    return df


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    """Convert text columns that are really numbers or dates."""
    for column in df.columns:
        series = df[column]
        if not _is_text(series):
            continue

        non_null = series.dropna()
        if non_null.empty:
            continue

        numeric = pd.to_numeric(non_null, errors="coerce")
        if numeric.notna().mean() >= 0.95:
            df[column] = pd.to_numeric(series, errors="coerce")
            continue

        sample = non_null.astype(str).head(200)
        looks_like_date = sample.str.contains(DATE_PATTERN, regex=True).mean() >= 0.8
        if looks_like_date:
            parsed = pd.to_datetime(
                non_null.astype(str), errors="coerce", format="mixed"
            )
            if parsed.notna().mean() >= 0.95:
                df[column] = pd.to_datetime(
                    series, errors="coerce", format="mixed"
                )
                continue

        df[column] = series.astype("string").str.strip()

    return df


def clean_dataframe(
    df: pd.DataFrame,
    name: str = "dataset",
    fill_missing: bool = False,
) -> tuple[pd.DataFrame, dict]:
    """Clean a DataFrame and return it alongside a small report."""
    report: dict[str, object] = {"name": name}
    report["rows_before"] = int(len(df))
    report["columns_before"] = int(df.shape[1])

    df = df.copy()
    df.columns = normalise_columns(df.columns)

    # Trim text and turn placeholder strings into real missing values *before*
    # checking for empty rows and columns.
    for column in df.columns:
        if _is_text(df[column]):
            df[column] = df[column].astype("string").str.strip()
            df[column] = df[column].replace(list(MISSING_TOKENS), pd.NA)

    empty_rows = int(df.isna().all(axis=1).sum())
    empty_columns = int(df.isna().all(axis=0).sum())
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")

    duplicates = int(df.duplicated().sum())
    df = df.drop_duplicates().reset_index(drop=True)

    df = _coerce_types(df)

    filled: dict[str, str] = {}
    if fill_missing:
        for column in df.columns:
            missing = int(df[column].isna().sum())
            if missing == 0:
                continue

            if pd.api.types.is_numeric_dtype(df[column]):
                median = df[column].median()
                if pd.notna(median):
                    df[column] = df[column].fillna(median)
                    filled[column] = f"median ({median:g})"
            elif pd.api.types.is_datetime64_any_dtype(df[column]):
                continue
            else:
                df[column] = df[column].fillna("Not provided").astype("string")
                filled[column] = "'Not provided'"

    df = _downcast_integers(df)

    report["rows_after"] = int(len(df))
    report["columns_after"] = int(df.shape[1])
    report["empty_rows_removed"] = empty_rows
    report["empty_columns_removed"] = empty_columns
    report["duplicates_removed"] = duplicates
    report["missing_cells"] = int(df.isna().sum().sum())
    report["filled_columns"] = filled
    report["columns"] = list(df.columns)
    return df.reset_index(drop=True), report


# ---------------------------------------------------------------------------
# Saving & CLI
# ---------------------------------------------------------------------------
def save_processed(df: pd.DataFrame, source: Path, output_dir: Path) -> Path:
    """Write a cleaned dataset as UTF-8 CSV for the dashboard."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{source.stem}_clean.csv"
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    return output_path


def _print_report(report: dict, output_path: Path, fill_missing: bool) -> None:
    print(
        f"  rows: {report['rows_before']:,} -> {report['rows_after']:,} "
        f"({report['duplicates_removed']} duplicate(s), "
        f"{report['empty_rows_removed']} empty row(s) removed)"
    )
    print(
        f"  columns: {report['columns_before']} -> {report['columns_after']} "
        f"({report['empty_columns_removed']} empty column(s) removed)"
    )

    filled = report["filled_columns"]
    if filled:
        details = ", ".join(f"{column} = {how}" for column, how in filled.items())
        print(f"  filled missing values: {details}")

    missing = report["missing_cells"]
    if missing:
        note = " (still present by design)" if not fill_missing else ""
        print(f"  missing cells: {missing:,}{note}")

    try:
        shown = output_path.relative_to(PROJECT_ROOT)
    except ValueError:
        shown = output_path
    print(f"  saved -> {shown}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="File or folder to clean (default: data/raw).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Folder for cleaned CSVs (default: data/processed).",
    )
    parser.add_argument(
        "--fill-missing",
        action="store_true",
        help="Impute numeric columns with the median and text with 'Not provided'.",
    )
    args = parser.parse_args(argv)

    source = (args.input or RAW_DIR).resolve()
    output_dir = (args.output or PROCESSED_DIR).resolve()

    if not source.exists():
        print(f"Input not found: {source}")
        return 1

    if source.is_file():
        files = [source]
    else:
        files = sorted(
            path
            for path in source.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        )

    if not files:
        print(f"No supported files found in {source}")
        print("Add a CSV, XLSX, XLSM, or JSON file, then run this script again.")
        return 0

    print("MIS data cleaning")
    print(f"Input:  {source}")
    print(f"Output: {output_dir}")
    print(f"Found {len(files)} file(s).\n")

    failures = 0
    for path in files:
        print(f"{path.name}")
        try:
            raw = read_raw_file(path)
            cleaned, report = clean_dataframe(
                raw, name=path.stem, fill_missing=args.fill_missing
            )
            output_path = save_processed(cleaned, path, output_dir)
            _print_report(report, output_path, args.fill_missing)
        except Exception as exc:  # noqa: BLE001 - keep processing other files
            failures += 1
            print(f"  ! failed: {exc}")
        print()

    if failures:
        print(f"Finished with {failures} file(s) that could not be cleaned.")
        return 1

    print("Done. Open the dashboard with: python -m streamlit run dashboard/app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
