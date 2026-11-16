"""
Ελέγχει αν έχει λυθεί κάθε διόρθωση του καταλόγου, κοιτώντας το ίδιο το site
(κωδικούς απάντησης, headers, HTML). Κάνει μόνο αιτήματα ανάγνωσης, όπως ένας
browser, δεν αλλάζει τίποτα.

Κάθε διόρθωση στο fixes.yaml έχει ένα πεδίο check που λέει τι να ελεγχθεί.

    python src/checks.py --site https://edge.dmsm2026.neuroverse.gr
    python src/checks.py --site https://clone1.edge.dmsm2026.neuroverse.gr --audit-id semrush_2026-11-09

Χρησιμοποιείται και από το timer_app.py.
"""
import argparse
import html
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXES_FILE = ROOT / "catalog" / "fixes.yaml"
DB_FILE = ROOT / "data" / "processed" / "audit.db"

USER_AGENT = "seo-fix-planner/0.1 (MSc thesis research)"
TIMEOUT = 20


# --- αιτήματα -----------------------------------------------------------------

class Response:
    def __init__(self, url, status, headers, body, error=None):
        self.url = url          # το τελικό URL, μετά από ανακατευθύνσεις
        self.status = status    # π.χ. 200, 404, ή None αν δεν απάντησε
        self.headers = headers  # λεξικό με μικρά γράμματα στα ονόματα
        self.body = body
        self.error = error


def build_url(site, path, bust_cache=False):
    """Ενώνει τη διεύθυνση του site με ένα path και κωδικοποιεί τα ελληνικά."""
    path = quote(path, safe="/%:?=&-._~")
    url = site.rstrip("/") + "/" + path.lstrip("/")
    if bust_cache:
        # παράμετρος που αγνοεί το WordPress, για να μη δούμε σελίδα από το cache
        url += ("&" if "?" in url else "?") + f"_sfp={int(time.time())}"
    return url


def fetch(url, read_body=True):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Cache-Control": "no-cache",
        "Accept-Encoding": "identity",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", errors="replace") if read_body else ""
            headers = {k.lower(): v for k, v in r.headers.items()}
            return Response(r.geturl(), r.status, headers, body)
    except urllib.error.HTTPError as e:
        headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return Response(url, e.code, headers, "")
    except Exception as e:  # δεν απάντησε, timeout, πρόβλημα SSL κ.λπ.
        return Response(url, None, {}, "", error=str(e))


# --- βοηθητικά για HTML --------------------------------------------------------

class AssetParser(HTMLParser):
    """Μαζεύει τα CSS (<link rel=stylesheet>) και JS (<script src>) μιας σελίδας."""

    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link" and "stylesheet" in (a.get("rel") or "").lower() and a.get("href"):
            self.assets.append(a["href"])
        elif tag == "script" and a.get("src"):
            self.assets.append(a["src"])


def visible_text(page_html):
    """Κείμενο της σελίδας χωρίς κώδικα. Προσέγγιση αυτού που μετράει το Semrush."""
    t = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", page_html)
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


# --- οι έλεγχοι ----------------------------------------------------------------
# Κάθε έλεγχος επιστρέφει (passed, total, detail):
#   passed / total  πόσα από τα επιμέρους πέρασαν (π.χ. 12 από τα 19 URLs)
#   detail          σύντομη περιγραφή για την οθόνη και το ημερολόγιο
# Η διόρθωση θεωρείται λυμένη όταν passed == total και total > 0.

def check_status(site, spec, audit_paths):
    expect = spec.get("expect", 200)
    paths = list(spec.get("paths", []))
    if spec.get("paths_from_issue"):
        paths += audit_paths.get(spec["paths_from_issue"], [])
    paths = list(dict.fromkeys(paths))  # χωρίς διπλά
    if not paths:
        return 0, 0, "δεν υπάρχουν URLs για έλεγχο"
    bad = []
    for p in paths:
        r = fetch(build_url(site, p, bust_cache=True), read_body=False)
        if r.status != expect:
            bad.append(f"{p} ({r.status or r.error})")
    detail = "όλα " + str(expect) if not bad else "όχι ακόμα: " + ", ".join(bad[:3])
    if len(bad) > 3:
        detail += f" και άλλα {len(bad) - 3}"
    return len(paths) - len(bad), len(paths), detail


def check_header(site, spec, audit_paths):
    header = spec["header"].lower()
    r = fetch(build_url(site, spec.get("path", "/"), bust_cache=True), read_body=False)
    if r.status is None:
        return 0, 1, f"δεν απάντησε ({r.error})"
    if header in r.headers:
        return 1, 1, f"{spec['header']}: {r.headers[header]}"
    return 0, 1, f"λείπει το {spec['header']}"


def check_assets_header(site, spec, audit_paths):
    """Το header πρέπει να υπάρχει σε όλα τα CSS/JS του site που φορτώνει η σελίδα."""
    header = spec["header"].lower()
    page = fetch(build_url(site, spec.get("page", "/"), bust_cache=True))
    if page.status != 200:
        return 0, 0, f"η σελίδα δεν φόρτωσε ({page.status or page.error})"
    parser = AssetParser()
    parser.feed(page.body)
    host = urlsplit(site).netloc
    assets = []
    for a in parser.assets:
        full = urljoin(page.url, a)
        if not spec.get("same_host_only", True) or urlsplit(full).netloc == host:
            assets.append(full)
    assets = list(dict.fromkeys(assets))
    if not assets:
        return 0, 0, "δεν βρέθηκαν CSS/JS"
    missing = []
    for url in assets:
        r = fetch(url, read_body=False)
        if header not in r.headers:
            missing.append(urlsplit(url).path.rsplit("/", 1)[-1])
    detail = "όλα έχουν " + spec["header"] if not missing else \
        f"λείπει από {len(missing)}: " + ", ".join(missing[:3])
    return len(assets) - len(missing), len(assets), detail


def check_meta_description(site, spec, audit_paths):
    r = fetch(build_url(site, spec.get("path", "/"), bust_cache=True))
    if r.status != 200:
        return 0, 1, f"η σελίδα δεν φόρτωσε ({r.status or r.error})"
    for tag in re.findall(r"(?is)<meta\b[^>]*>", r.body):
        if re.search(r"""name\s*=\s*["']description["']""", tag, re.I):
            m = re.search(r"""content\s*=\s*["']([^"']*)["']""", tag, re.I)
            if m and m.group(1).strip():
                return 1, 1, "υπάρχει: " + m.group(1).strip()[:60]
            return 0, 1, "υπάρχει αλλά είναι κενό"
    return 0, 1, "δεν υπάρχει"


def check_content(site, spec, audit_paths):
    """Λέξεις και αναλογία κειμένου προς HTML. Προσέγγιση των ορίων του Semrush."""
    r = fetch(build_url(site, spec.get("path", "/"), bust_cache=True))
    if r.status != 200:
        return 0, 2, f"η σελίδα δεν φόρτωσε ({r.status or r.error})"
    text = visible_text(r.body)
    words = len(text.split())
    ratio = len(text) / max(len(r.body), 1)
    passed = 0
    if words >= spec.get("min_words", 200):
        passed += 1
    if ratio > spec.get("min_text_ratio", 0.10):
        passed += 1
    return passed, 2, f"{words} λέξεις, αναλογία {ratio:.1%}"


CHECKS = {
    "status": check_status,
    "header": check_header,
    "assets_header": check_assets_header,
    "meta_description": check_meta_description,
    "content": check_content,
}


# --- κατάλογος και audit -------------------------------------------------------

def load_fixes():
    with open(FIXES_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)["fixes"]


def load_audit_paths(audit_id=None):
    """Για κάθε πρόβλημα, τα paths των σελίδων που το έχουν, από το audit.db."""
    if not DB_FILE.exists():
        return {}
    con = sqlite3.connect(DB_FILE)
    try:
        if audit_id is None:
            row = con.execute(
                "SELECT audit_id FROM issue_occurrences ORDER BY audit_date DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return {}
            audit_id = row[0]
        rows = con.execute(
            "SELECT issue, url FROM issue_occurrences WHERE audit_id = ?", (audit_id,)
        ).fetchall()
    finally:
        con.close()
    out = {}
    for issue, url in rows:
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        out.setdefault(issue, []).append(path)
    return out


def run_check(site, fix, audit_paths):
    spec = fix.get("check")
    if not spec:
        return 0, 0, "δεν έχει οριστεί check"
    func = CHECKS.get(spec.get("type"))
    if func is None:
        return 0, 0, f"άγνωστος τύπος check: {spec.get('type')}"
    try:
        return func(site, spec, audit_paths)
    except Exception as e:
        return 0, 0, f"σφάλμα στον έλεγχο: {e}"


def is_solved(passed, total):
    return total > 0 and passed == total


def main():
    parser = argparse.ArgumentParser(description="Ελέγχει ποιες διορθώσεις φαίνονται λυμένες.")
    parser.add_argument("--site", required=True, help="π.χ. https://clone1.edge.dmsm2026.neuroverse.gr")
    parser.add_argument("--audit-id", help="από ποιο audit να πάρει τα URLs (προεπιλογή: το πιο πρόσφατο)")
    args = parser.parse_args()

    audit_paths = load_audit_paths(args.audit_id)
    if not audit_paths:
        print("Προσοχή: δεν βρέθηκε audit στη βάση, οι έλεγχοι με paths_from_issue θα είναι κενοί.\n")

    for fix in load_fixes():
        passed, total, detail = run_check(args.site, fix, audit_paths)
        mark = "OK " if is_solved(passed, total) else "-- "
        print(f"{mark} {fix['id']:<28} {passed}/{total}  {detail}")


if __name__ == "__main__":
    sys.exit(main())
