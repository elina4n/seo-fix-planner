"""
Ελέγχει τον κατάλογο διορθώσεων (catalog/fixes.yaml) και δείχνει ποιες
διορθώσεις χρειάζονται για ένα audit και πόσα προβλήματα λύνει η καθεμία.

    python src/catalog_check.py
    python src/catalog_check.py --audit-id semrush_2026-10-04
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXES_FILE = ROOT / "catalog" / "fixes.yaml"
SEVERITY_FILE = ROOT / "catalog" / "severity_semrush.yaml"
DB_FILE = ROOT / "data" / "processed" / "audit.db"


def load_fixes():
    data = yaml.safe_load(FIXES_FILE.read_text(encoding="utf-8")) or {}
    return data.get("fixes", [])


def load_audit(audit_id):
    if not DB_FILE.exists():
        sys.exit("Δεν υπάρχει βάση. Τρέξε πρώτα το ingest.py.")
    with sqlite3.connect(DB_FILE) as con:
        if audit_id is None:
            # Χωρίς --audit-id παίρνουμε το πιο πρόσφατο
            row = con.execute(
                "SELECT audit_id FROM issue_occurrences ORDER BY audit_date DESC LIMIT 1"
            ).fetchone()
            if row is None:
                sys.exit("Η βάση είναι άδεια.")
            audit_id = row[0]
        df = pd.read_sql(
            "SELECT * FROM issue_occurrences WHERE audit_id = ?", con, params=(audit_id,)
        )
    if df.empty:
        sys.exit(f"Δεν βρέθηκε το audit: {audit_id}")
    return audit_id, df


def validate_catalog(fixes):
    problems = []
    known_issues = set((yaml.safe_load(SEVERITY_FILE.read_text(encoding="utf-8")) or {}).keys())
    ids = [f["id"] for f in fixes]

    for dup in {i for i in ids if ids.count(i) > 1}:
        problems.append(f"το id {dup} υπάρχει δύο φορές")

    for f in fixes:
        for issue in f.get("resolves", []):
            if issue not in known_issues:
                problems.append(f"{f['id']}: το '{issue}' δεν υπάρχει στο severity_semrush.yaml")
        for dep in f.get("depends_on", []):
            if dep not in ids:
                problems.append(f"{f['id']}: εξαρτάται από το '{dep}', που δεν υπάρχει")

    # Κύκλοι: π.χ. η F01 χρειάζεται την F03 και η F03 την F01
    graph = {f["id"]: f.get("depends_on", []) for f in fixes}
    state = {}

    def visit(node, path):
        if state.get(node) == "visiting":
            problems.append("κύκλος εξαρτήσεων: " + " -> ".join(path + [node]))
            return
        if state.get(node) == "done" or node not in graph:
            return
        state[node] = "visiting"
        for nxt in graph[node]:
            visit(nxt, path + [node])
        state[node] = "done"

    for node in graph:
        visit(node, [])
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-id")
    args = parser.parse_args()

    fixes = load_fixes()
    problems = validate_catalog(fixes)
    print("Έλεγχος καταλόγου")
    if problems:
        for p in problems:
            print(f"  Πρόβλημα: {p}")
    else:
        print(f"  OK, {len(fixes)} διορθώσεις")

    audit_id, audit = load_audit(args.audit_id)
    per_issue = audit.groupby("issue")["occurrences"].sum()
    present = set(per_issue.index)

    rows = []
    for f in fixes:
        hits = [i for i in f.get("resolves", []) if i in present]
        if hits:
            rows.append(
                {
                    "fix": f["id"],
                    "where": f.get("where", ""),
                    "issue_types": len(hits),
                    "occurrences": int(per_issue[hits].sum()),
                    "minutes": f.get("minutes"),
                    "depends_on": ", ".join(f.get("depends_on", [])) or "-",
                }
            )
    columns = ["fix", "where", "issue_types", "occurrences", "minutes", "depends_on"]
    plan = pd.DataFrame(rows, columns=columns).sort_values("occurrences", ascending=False)

    covered = {i for f in fixes for i in f.get("resolves", [])} & present
    uncovered = sorted(present - covered)

    print(f"\nΚάλυψη του audit {audit_id}")
    if uncovered:
        print("  Δεν υπάρχει διόρθωση για:")
        for i in uncovered:
            print(f"  - {i} ({int(per_issue[i])} εμφανίσεις)")
    else:
        print("  Όλα τα προβλήματα έχουν διόρθωση στον κατάλογο")

    print("\nΔιορθώσεις για αυτό το audit\n")
    print(plan.to_string(index=False))

    # Πόσες εμφανίσεις λύνονται από διορθώσεις που καλύπτουν πάνω από έναν τύπο
    total = int(per_issue.sum())
    multi = plan[plan["issue_types"] > 1]
    multi_ids = set(multi["fix"])
    multi_issues = {i for f in fixes if f["id"] in multi_ids
                    for i in f.get("resolves", []) if i in present}
    multi_occ = int(per_issue[sorted(multi_issues)].sum()) if multi_issues else 0

    print("\nΣύνοψη")
    print(f"  Τύποι issues:                      {len(present)}")
    print(f"  Εμφανίσεις:                        {total}")
    print(f"  Διορθώσεις που χρειάζονται:        {len(plan)}")
    print(f"  Διορθώσεις που λύνουν >1 τύπο:     {len(multi)}")
    print(f"  Εμφανίσεις που λύνονται από αυτές: {multi_occ} ({multi_occ / total:.0%})")


if __name__ == "__main__":
    main()
