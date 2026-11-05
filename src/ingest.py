"""
ingest.py — Διαβάζει ένα export ελέγχου (Semrush Site Audit "mega export")
και το αποθηκεύει σε "μακριά" μορφή: μία γραμμή ανά (σελίδα, issue).

Τι κάνει, βήμα-βήμα:
  1. Διαβάζει το αρχείο (.xlsx ή .csv). Η πρώτη στήλη είναι το URL της σελίδας,
     κάθε άλλη στήλη είναι ένας τύπος issue με τον αριθμό εμφανίσεων.
  2. Καθαρίζει τα ονόματα των issues (π.χ. το "&#39;" γίνεται "'").
  3. Κρατά μόνο τα ζεύγη (σελίδα, issue) με τουλάχιστον μία εμφάνιση.
  4. Προσθέτει τη σοβαρότητα κάθε issue από το catalog/severity_semrush.yaml.
  5. Αποθηκεύει σε CSV και στη βάση SQLite data/processed/audit.db,
     με ένα audit_id ώστε να κρατάμε πολλά audits (πριν / μετά τις διορθώσεις).

Χρήση:
    python src/ingest.py data/raw/edge.dmsm2026.neuroverse.gr_mega_export_20261004.xlsx
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


def clean_issue_name(name: str) -> str:
    """Καθαρίζει ένα όνομα issue: HTML entities και περιττά κενά."""
    name = html.unescape(str(name)).strip()
    return re.sub(r"\s+", " ", name)


def read_export(path: Path) -> pd.DataFrame:
    """Διαβάζει το export και δίνει στήλες: url + μία στήλη ανά issue."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # το Semrush xlsx βγάζει άσχετο warning για styles
        if path.suffix.lower() == ".csv":
            df = pd.read_csv(path)
        else:
            df = pd.read_excel(path)
    df.columns = ["url"] + [clean_issue_name(c) for c in df.columns[1:]]
    return df


def to_long(df: pd.DataFrame) -> pd.DataFrame:
    """Από πίνακα 'σελίδες × issues' σε μία γραμμή ανά (σελίδα, issue)."""
    long = df.melt(id_vars="url", var_name="issue", value_name="occurrences")
    long["occurrences"] = (
        pd.to_numeric(long["occurrences"], errors="coerce").fillna(0).astype(int)
    )
    return long[long["occurrences"] > 0].reset_index(drop=True)


def audit_date_from_filename(path: Path) -> str:
    """Βρίσκει ημερομηνία ΕΕΕΕΜΜΗΗ στο όνομα αρχείου (π.χ. ..._20261004.xlsx)."""
    m = re.search(r"(\d{4})(\d{2})(\d{2})", path.stem)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else "unknown-date"


def add_severity(long: pd.DataFrame) -> pd.DataFrame:
    """Προσθέτει επίπεδο σοβαρότητας (από το εργαλείο) και βάρος (από το config)."""
    severity = yaml.safe_load(SEVERITY_FILE.read_text(encoding="utf-8")) or {}
    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    weights = config.get("severity_weights", {})

    long["severity"] = long["issue"].map(severity).fillna("unknown")
    long["severity_weight"] = long["severity"].map(weights).fillna(weights.get("unknown", 1))

    unknown = sorted(long.loc[long["severity"] == "unknown", "issue"].unique())
    if unknown:
        print("\n⚠️  Issues χωρίς σοβαρότητα στο catalog/severity_semrush.yaml:")
        for name in unknown:
            print(f"   - {name}")
    return long


def save(long: pd.DataFrame, audit_id: str) -> Path:
    """Αποθηκεύει σε CSV και SQLite. Αν το audit_id υπάρχει ήδη, αντικαθίσταται."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = PROCESSED_DIR / f"{audit_id}_issues.csv"
    long.to_csv(csv_path, index=False, encoding="utf-8-sig")

    with sqlite3.connect(DB_FILE) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS issue_occurrences (
                   audit_id TEXT, source TEXT, audit_date TEXT, url TEXT,
                   issue TEXT, occurrences INTEGER, severity TEXT, severity_weight REAL)"""
        )
        con.execute("DELETE FROM issue_occurrences WHERE audit_id = ?", (audit_id,))
        long.to_sql("issue_occurrences", con, if_exists="append", index=False)
    return csv_path


def print_summary(long: pd.DataFrame) -> None:
    """Σύνοψη ανά issue, με τη σειρά σοβαρότητας του εργαλείου."""
    summary = (
        long.groupby(["issue", "severity", "severity_weight"])
        .agg(pages=("url", "nunique"), occurrences=("occurrences", "sum"))
        .reset_index()
        .sort_values(["severity_weight", "occurrences"], ascending=False)
    )
    print("\nIssues (σειρά σοβαρότητας του εργαλείου):\n")
    print(summary[["issue", "severity", "pages", "occurrences"]].to_string(index=False))
    print(
        f"\nΣύνολο: {summary.shape[0]} τύποι issues, "
        f"{int(summary['occurrences'].sum())} εμφανίσεις, "
        f"{long['url'].nunique()} σελίδες με issues."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Εισαγωγή export ελέγχου SEO.")
    parser.add_argument("export", type=Path, help="Αρχείο .xlsx ή .csv")
    parser.add_argument("--source", default="semrush", help="Εργαλείο προέλευσης")
    parser.add_argument("--audit-id", help="Προαιρετικό όνομα audit (π.χ. before / after_F01)")
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
    print(f"\n✅ audit_id = {audit_id}")
    print(f"   CSV:  {csv_path.relative_to(ROOT)}")
    print(f"   Βάση: {DB_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
