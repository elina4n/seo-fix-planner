"""
catalog_check.py — Ελέγχει τον κατάλογο διορθώσεων απέναντι σε ένα audit.

Απαντά σε τρία ερωτήματα:
  1. Είναι σωστός ο κατάλογος; (ορθογραφία issues, εξαρτήσεις που υπάρχουν, κύκλοι)
  2. Ποια issues του audit καλύπτονται από διόρθωση και ποια όχι;
  3. Πόσο συχνά μία διόρθωση λύνει πολλά issues; (πρώτη ένδειξη για το SQ1)

Χρήση:
    python src/catalog_check.py                  # το πιο πρόσφατο audit
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


def load_fixes() -> list[dict]:
    data = yaml.safe_load(FIXES_FILE.read_text(encoding="utf-8")) or {}
    return data.get("fixes", [])


def load_audit(audit_id: str | None) -> tuple[str, pd.DataFrame]:
    if not DB_FILE.exists():
        sys.exit("Δεν υπάρχει βάση. Τρέξε πρώτα: python src/ingest.py <export>")
    with sqlite3.connect(DB_FILE) as con:
        if audit_id is None:
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
        sys.exit(f"Δεν βρέθηκε το audit_id: {audit_id}")
    return audit_id, df


def validate_catalog(fixes: list[dict]) -> list[str]:
    """Επιστρέφει λίστα με προβλήματα στον κατάλογο."""
    problems = []
    known_issues = set((yaml.safe_load(SEVERITY_FILE.read_text(encoding="utf-8")) or {}).keys())
    ids = [f["id"] for f in fixes]

    for dup in {i for i in ids if ids.count(i) > 1}:
        problems.append(f"Διπλό id διόρθωσης: {dup}")

    for f in fixes:
        for issue in f.get("resolves", []):
            if issue not in known_issues:
                problems.append(
                    f"{f['id']}: το issue '{issue}' δεν υπάρχει στο severity_semrush.yaml "
                    "(ορθογραφικό λάθος;)"
                )
        for dep in f.get("depends_on", []):
            if dep not in ids:
                problems.append(f"{f['id']}: εξαρτάται από άγνωστη διόρθωση '{dep}'")

    # Έλεγχος για κύκλους στις εξαρτήσεις (Α χρειάζεται Β, Β χρειάζεται Α)
    graph = {f["id"]: f.get("depends_on", []) for f in fixes}
    state = {}  # 1 = σε εξέλιξη, 2 = έτοιμο

    def visit(node, path):
        if state.get(node) == 1:
            problems.append("Κύκλος εξαρτήσεων: " + " → ".join(path + [node]))
            return
        if state.get(node) == 2 or node not in graph:
            return
        state[node] = 1
        for nxt in graph[node]:
            visit(nxt, path + [node])
        state[node] = 2

    for node in graph:
        visit(node, [])
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="Έλεγχος καταλόγου διορθώσεων.")
    parser.add_argument("--audit-id", help="Ποιο audit να ελεγχθεί (default: το πιο πρόσφατο)")
    args = parser.parse_args()

    fixes = load_fixes()
    problems = validate_catalog(fixes)
    print("1) Έλεγχος καταλόγου")
    if problems:
        for p in problems:
            print(f"   ❌ {p}")
    else:
        print(f"   ✅ {len(fixes)} διορθώσεις, χωρίς προβλήματα")

    audit_id, audit = load_audit(args.audit_id)
    per_issue = audit.groupby("issue")["occurrences"].sum()
    present = set(per_issue.index)

    # Ποιες διορθώσεις αφορούν αυτό το audit και τι καλύπτει η καθεμία
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

    print(f"\n2) Κάλυψη του audit {audit_id}")
    if uncovered:
        print("   Issues χωρίς διόρθωση στον κατάλογο:")
        for i in uncovered:
            print(f"   - {i} ({int(per_issue[i])} εμφανίσεις)")
    else:
        print("   ✅ Όλα τα issues καλύπτονται από τουλάχιστον μία διόρθωση")

    print("\n3) Διορθώσεις που αφορούν αυτό το audit\n")
    print(plan.to_string(index=False))

    total = int(per_issue.sum())
    multi = plan[plan["issue_types"] > 1]
    # Τύποι issues που λύνονται από διόρθωση με >1 τύπο (χωρίς διπλομέτρηση)
    multi_ids = set(multi["fix"])
    multi_issues = {i for f in fixes if f["id"] in multi_ids
                    for i in f.get("resolves", []) if i in present}
    multi_occ = int(per_issue[sorted(multi_issues)].sum()) if multi_issues else 0

    print("\nΣύνοψη")
    print(f"   Τύποι issues:                     {len(present)}")
    print(f"   Εμφανίσεις issues:                {total}")
    print(f"   Διορθώσεις που χρειάζονται:       {len(plan)}")
    print(f"   Διορθώσεις που λύνουν >1 τύπο:    {len(multi)}")
    print(f"   Εμφανίσεις που λύνονται από αυτές: {multi_occ} ({multi_occ / total:.0%})")


if __name__ == "__main__":
    main()
