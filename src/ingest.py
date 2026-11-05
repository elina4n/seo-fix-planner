"""
Διαβάζει το export του Semrush Site Audit και το αποθηκεύει ως μία γραμμή
ανά σελίδα και πρόβλημα, μαζί με τη σοβαρότητα του προβλήματος.

    python src/ingest.py data/raw/<export>.xlsx
    python src/ingest.py data/raw/<export>.xlsx --audit-id after_F01
"""

import argparse
import html
import re
import sqlite3
import sys
import warnings
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
SEVERITY_FILE = ROOT / "catalog" / "severity_semrush.yaml"
CONFIG_FILE = ROOT / "catalog" / "config.yaml"
PROCESSED_DIR = ROOT / "data" / "processed"
DB_FILE = PROCESSED_DIR / "audit.db"


def clean_issue_name(name):
    # Το Semrush γράφει κάποια σύμβολα ως HTML (π.χ. &#39; αντί για ')
    name = html.unescape(str(name)).strip()
    return re.sub(r"\s+", " ", name)


def read_export(path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if path.suffix.lower() == ".csv":
            df = pd.read_csv(path)
        else:
            df = pd.read_excel(path)
    # Πρώτη στήλη: η σελίδα. Οι υπόλοιπες: ένα πρόβλημα η καθεμία.
    df.columns = ["url"] + [clean_issue_name(c) for c in df.columns[1:]]
    return df


def to_long(df):
    # Από πίνακα σελίδες x προβλήματα σε μία γραμμή ανά (σελίδα, πρόβλημα)
    long = df.melt(id_vars="url", var_name="issue", value_name="occurrences")
    long["occurrences"] = (
        pd.to_numeric(long["occurrences"], errors="coerce").fillna(0).astype(int)
    )
    return long[long["occurrences"] > 0].reset_index(drop=True)


def audit_date_from_filename(path):
    # Η ημερομηνία από το όνομα του αρχείου, π.χ. ..._20261004.xlsx
    m = re.search(r"(\d{4})(\d{2})(\d{2})", path.stem)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else "unknown-date"


def add_severity(long):
    severity = yaml.safe_load(SEVERITY_FILE.read_text(encoding="utf-8")) or {}
    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    weights = config.get("severity_weights", {})

    long["severity"] = long["issue"].map(severity).fillna("unknown")
    long["severity_weight"] = long["severity"].map(weights).fillna(weights.get("unknown", 1))

    unknown = sorted(long.loc[long["severity"] == "unknown", "issue"].unique())
    if unknown:
        print("\nΠροσοχή, δεν υπάρχει σοβαρότητα στο severity_semrush.yaml για:")
        for name in unknown:
            print(f"  - {name}")
    return long


def save(long, audit_id):
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = PROCESSED_DIR / f"{audit_id}_issues.csv"
    long.to_csv(csv_path, index=False, encoding="utf-8-sig")

    with sqlite3.connect(DB_FILE) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS issue_occurrences (
                   audit_id TEXT, source TEXT, audit_date TEXT, url TEXT,
                   issue TEXT, occurrences INTEGER, severity TEXT, severity_weight REAL)"""
        )
        # Αν το ίδιο audit έχει ξαναμπεί, σβήνεται και γράφεται από την αρχή
        con.execute("DELETE FROM issue_occurrences WHERE audit_id = ?", (audit_id,))
        long.to_sql("issue_occurrences", con, if_exists="append", index=False)
    return csv_path


def print_summary(long):
    summary = (
        long.groupby(["issue", "severity", "severity_weight"])
        .agg(pages=("url", "nunique"), occurrences=("occurrences", "sum"))
        .reset_index()
        .sort_values(["severity_weight", "occurrences"], ascending=False)
    )
    print("\nΠροβλήματα, με τη σειρά σοβαρότητας του Semrush:\n")
    print(summary[["issue", "severity", "pages", "occurrences"]].to_string(index=False))
    print(
        f"\nΣύνολο: {summary.shape[0]} τύποι issues, "
        f"{int(summary['occurrences'].sum())} εμφανίσεις, "
        f"{long['url'].nunique()} σελίδες με issues."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("export", type=Path, help="το αρχείο .xlsx ή .csv")
    parser.add_argument("--source", default="semrush")
    parser.add_argument("--audit-id", help="όνομα για το audit, π.χ. before ή after_F01")
    args = parser.parse_args()

    if not args.export.exists():
        sys.exit(f"Δεν βρέθηκε το αρχείο: {args.export}")

    audit_date = audit_date_from_filename(args.export)
    audit_id = args.audit_id or f"{args.source}_{audit_date}"

    long = to_long(read_export(args.export))
    long.insert(0, "audit_date", audit_date)
    long.insert(0, "source", args.source)
    long.insert(0, "audit_id", audit_id)
    long = add_severity(long)

    csv_path = save(long, audit_id)
    print_summary(long)
    print(f"\nΑποθηκεύτηκε ως audit '{audit_id}'")
    print(f"  {csv_path.relative_to(ROOT)}")
    print(f"  {DB_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
