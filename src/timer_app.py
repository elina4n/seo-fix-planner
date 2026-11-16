"""
Χρονόμετρο για τις δοκιμές στους κλώνους.

Πατάς Start μία φορά. Μετά το πρόγραμμα ελέγχει μόνο του κάθε λίγα δευτερόλεπτα
το site (με τους ελέγχους του checks.py) και κρατάει τη στιγμή που λύθηκε η κάθε
διόρθωση. Το Pause σταματάει το ρολόι όσο περιμένεις κάτι που δεν μετράει.

    python src/timer_app.py

Γράφει δύο αρχεία στο data/timings/ για κάθε δοκιμή:
    <δοκιμή>_events.csv   ό,τι έγινε, με την ώρα του
    <δοκιμή>_summary.csv  ένας χρόνος ανά διόρθωση
"""
import csv
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import checks

ROOT = Path(__file__).resolve().parents[1]
TIMINGS_DIR = ROOT / "data" / "timings"

POLL_SECONDS = 15   # κάθε πόσο ελέγχει το site
CONFIRM = 2         # πόσους συνεχόμενους επιτυχημένους ελέγχους θέλει για "λύθηκε"


def now():
    return time.time()


def iso(t):
    return datetime.fromtimestamp(t).isoformat(timespec="seconds")


def mmss(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class Session:
    """Μία δοκιμή: ένας κλώνος, μία σειρά (model / semrush / practice)."""

    def __init__(self, site, clone, order, audit_id=None, fixes=None):
        self.site = site
        self.clone = clone
        self.order = order
        self.audit_id = audit_id
        self.fixes = fixes if fixes is not None else checks.load_fixes()
        self.audit_paths = checks.load_audit_paths(audit_id)

        self.id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{clone}_{order}"
        self.lock = threading.Lock()
        self.events = []
        self.start_time = None
        self.stop_time = None
        self.pauses = []           # [(αρχή, τέλος ή None)]
        self.state = {}            # fix_id -> στοιχεία για την οθόνη

        for f in self.fixes:
            self.state[f["id"]] = {
                "baseline_ok": None,   # ήταν ήδη λυμένο στην αρχή;
                "passed": 0, "total": 0, "detail": "",
                "streak": 0,           # συνεχόμενοι επιτυχημένοι έλεγχοι
                "first_pass": None,    # πρώτη φορά που πέρασε
                "solved_at": None,     # επιβεβαιωμένη λύση (= first_pass)
            }

    # --- χρόνος ---

    def paused(self):
        return bool(self.pauses) and self.pauses[-1][1] is None

    def net(self, t):
        """Καθαρός χρόνος από την αρχή μέχρι τη στιγμή t, χωρίς τις παύσεις."""
        if self.start_time is None:
            return 0.0
        paused = 0.0
        for a, b in self.pauses:
            b = b if b is not None else t
            if a < t:
                paused += min(b, t) - a
        return (t - self.start_time) - paused

    # --- καταγραφή ---

    def log(self, event, fix_id="", detail="", t=None):
        t = t if t is not None else now()
        self.events.append({
            "time": iso(t),
            "net_seconds": round(self.net(t), 1),
            "event": event,
            "fix": fix_id,
            "detail": detail,
        })

    # --- ενέργειες ---

    def start(self):
        with self.lock:
            self.start_time = now()
            self.log("start", detail=f"site={self.site} clone={self.clone} order={self.order}")
        # αρχικός έλεγχος: τι είναι ήδη λυμένο πριν ξεκινήσεις
        for f in self.fixes:
            passed, total, detail = checks.run_check(self.site, f, self.audit_paths)
            with self.lock:
                s = self.state[f["id"]]
                s.update(passed=passed, total=total, detail=detail)
                s["baseline_ok"] = checks.is_solved(passed, total)
                self.log("baseline", f["id"], f"{passed}/{total} {detail}")

    def pause(self):
        with self.lock:
            if not self.paused():
                self.pauses.append((now(), None))
                self.log("pause")

    def resume(self):
        with self.lock:
            if self.paused():
                a, _ = self.pauses[-1]
                self.pauses[-1] = (a, now())
                self.log("resume")

    def note(self, text):
        with self.lock:
            self.log("note", detail=text)

    def poll(self):
        """Ένας γύρος ελέγχων. Επιστρέφει τις διορθώσεις που μόλις λύθηκαν."""
        newly = []
        for f in self.fixes:
            s = self.state[f["id"]]
            if s["solved_at"] is not None or s["baseline_ok"]:
                continue
            t = now()
            passed, total, detail = checks.run_check(self.site, f, self.audit_paths)
            with self.lock:
                if (passed, total) != (s["passed"], s["total"]):
                    self.log("progress", f["id"], f"{passed}/{total} {detail}", t)
                s.update(passed=passed, total=total, detail=detail)
                if checks.is_solved(passed, total):
                    if s["streak"] == 0:
                        s["first_pass"] = t
                    s["streak"] += 1
                    if s["streak"] >= CONFIRM:
                        s["solved_at"] = s["first_pass"]
                        self.log("solved", f["id"], detail, s["solved_at"])
                        newly.append(f["id"])
                else:
                    s["streak"] = 0
                    s["first_pass"] = None
        return newly

    def stop(self):
        with self.lock:
            if self.paused():
                a, _ = self.pauses[-1]
                self.pauses[-1] = (a, now())
            self.stop_time = now()
            self.log("stop")
        return self.save()

    # --- αρχεία ---

    def summary_rows(self):
        """Ένας χρόνος ανά διόρθωση, με τη σειρά που λύθηκαν.
        Ο χρόνος μιας διόρθωσης μετράει από τη λύση της προηγούμενης (ή την αρχή)."""
        solved = [(fid, s) for fid, s in self.state.items() if s["solved_at"] is not None]
        solved.sort(key=lambda x: x[1]["solved_at"])
        rows, previous = [], 0.0
        for rank, (fid, s) in enumerate(solved, 1):
            at = self.net(s["solved_at"])
            rows.append({
                "session": self.id, "clone": self.clone, "order": self.order,
                "fix": fid, "solved_rank": rank,
                "solved_time": iso(s["solved_at"]),
                "net_from_start_s": round(at, 1),
                "net_duration_s": round(at - previous, 1),
                "baseline_ok": s["baseline_ok"], "detail": s["detail"],
            })
            previous = at
        for fid, s in self.state.items():
            if s["solved_at"] is None:
                rows.append({
                    "session": self.id, "clone": self.clone, "order": self.order,
                    "fix": fid, "solved_rank": "",
                    "solved_time": "", "net_from_start_s": "", "net_duration_s": "",
                    "baseline_ok": s["baseline_ok"],
                    "detail": ("ήταν ήδη λυμένο στην αρχή: " if s["baseline_ok"] else "δεν λύθηκε: ")
                              + s["detail"],
                })
        return rows

    def save(self):
        TIMINGS_DIR.mkdir(parents=True, exist_ok=True)
        events_file = TIMINGS_DIR / f"{self.id}_events.csv"
        summary_file = TIMINGS_DIR / f"{self.id}_summary.csv"
        with self.lock:
            with open(events_file, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["time", "net_seconds", "event", "fix", "detail"])
                w.writeheader()
                w.writerows(self.events)
            rows = self.summary_rows()
            with open(summary_file, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
        return events_file, summary_file


# --- το παράθυρο ----------------------------------------------------------------

def run_gui():
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("seo-fix-planner χρονόμετρο")
    root.attributes("-topmost", True)

    session = None
    updates = queue.Queue()
    worker_stop = threading.Event()

    # πεδία
    form = ttk.Frame(root, padding=8)
    form.pack(fill="x")
    site_var = tk.StringVar(value="https://clone1.edge.dmsm2026.neuroverse.gr")
    clone_var = tk.StringVar(value="clone1")
    order_var = tk.StringVar(value="model")
    audit_var = tk.StringVar(value="")
    for i, (label, var) in enumerate([("Site", site_var), ("Κλώνος", clone_var),
                                      ("Audit id (προαιρ.)", audit_var)]):
        ttk.Label(form, text=label).grid(row=i, column=0, sticky="w")
        ttk.Entry(form, textvariable=var, width=45).grid(row=i, column=1, sticky="we")
    ttk.Label(form, text="Σειρά").grid(row=3, column=0, sticky="w")
    ttk.Combobox(form, textvariable=order_var, values=["model", "semrush", "practice"],
                 state="readonly", width=12).grid(row=3, column=1, sticky="w")

    # κουμπιά και ρολόι
    bar = ttk.Frame(root, padding=(8, 0))
    bar.pack(fill="x")
    start_btn = ttk.Button(bar, text="Start")
    pause_btn = ttk.Button(bar, text="Pause", state="disabled")
    stop_btn = ttk.Button(bar, text="Stop", state="disabled")
    for b in (start_btn, pause_btn, stop_btn):
        b.pack(side="left", padx=2)
    clock = ttk.Label(bar, text="00:00", font=("Segoe UI", 16, "bold"))
    clock.pack(side="right")

    # πίνακας διορθώσεων
    cols = ("status", "progress", "time", "detail")
    tree = ttk.Treeview(root, columns=cols, show="tree headings", height=7)
    tree.heading("#0", text="Διόρθωση")
    tree.heading("status", text="Κατάσταση")
    tree.heading("progress", text="Πρόοδος")
    tree.heading("time", text="Λύθηκε στο")
    tree.heading("detail", text="Λεπτομέρεια")
    tree.column("#0", width=215)
    tree.column("status", width=90)
    tree.column("progress", width=65, anchor="center")
    tree.column("time", width=90, anchor="center")
    tree.column("detail", width=280)
    tree.pack(fill="both", expand=True, padx=8, pady=6)

    # σημειώσεις
    note_bar = ttk.Frame(root, padding=(8, 0, 8, 8))
    note_bar.pack(fill="x")
    note_var = tk.StringVar()
    ttk.Entry(note_bar, textvariable=note_var).pack(side="left", fill="x", expand=True)
    note_btn = ttk.Button(note_bar, text="Σημείωση", state="disabled")
    note_btn.pack(side="left", padx=(4, 0))

    def refresh_table():
        if session is None:
            return
        tree.delete(*tree.get_children())
        with session.lock:
            for fid, s in session.state.items():
                if s["baseline_ok"]:
                    status = "ήδη λυμένο"
                elif s["solved_at"] is not None:
                    status = "ΛΥΘΗΚΕ"
                elif s["baseline_ok"] is None:
                    status = "έλεγχος..."
                else:
                    status = "ανοιχτό"
                t = mmss(session.net(s["solved_at"])) if s["solved_at"] else ""
                tree.insert("", "end", text=fid,
                            values=(status, f"{s['passed']}/{s['total']}", t, s["detail"]))

    def tick():
        if session is not None and session.start_time is not None:
            end = session.stop_time or now()
            clock.config(text=mmss(session.net(end)) + ("  (παύση)" if session.paused() else ""))
        try:
            while True:
                kind, payload = updates.get_nowait()
                if kind == "solved":
                    root.bell()
                refresh_table()
                if kind == "already":
                    messagebox.showwarning(
                        "Ήδη λυμένα",
                        "Αυτά φαίνονται λυμένα πριν ξεκινήσεις:\n" + "\n".join(payload) +
                        "\n\nΑν ο κλώνος έπρεπε να έχει το πρόβλημα, έλεγξέ τον πριν συνεχίσεις.")
        except queue.Empty:
            pass
        root.after(500, tick)

    def worker():
        session.start()
        updates.put(("refresh", None))
        already = [fid for fid, s in session.state.items() if s["baseline_ok"]]
        if already:
            updates.put(("already", already))
        while not worker_stop.is_set():
            for fid in session.poll():
                updates.put(("solved", fid))
            updates.put(("refresh", None))
            worker_stop.wait(POLL_SECONDS)

    def on_start():
        nonlocal session
        site = site_var.get().strip()
        if not site.startswith("http"):
            messagebox.showerror("Site", "Το site πρέπει να αρχίζει με https://")
            return
        session = Session(site, clone_var.get().strip() or "clone",
                          order_var.get(), audit_var.get().strip() or None)
        start_btn.config(state="disabled")
        pause_btn.config(state="normal")
        stop_btn.config(state="normal")
        note_btn.config(state="normal")
        worker_stop.clear()
        threading.Thread(target=worker, daemon=True).start()

    def on_pause():
        if session.paused():
            session.resume()
            pause_btn.config(text="Pause")
        else:
            session.pause()
            pause_btn.config(text="Resume")

    def on_stop():
        if not messagebox.askyesno("Stop", "Τέλος της δοκιμής;"):
            return
        worker_stop.set()
        events_file, summary_file = session.stop()
        refresh_table()
        for b in (pause_btn, stop_btn, note_btn):
            b.config(state="disabled")
        messagebox.showinfo("Αποθηκεύτηκε", f"{events_file.name}\n{summary_file.name}\n\nστο data/timings/")

    def on_note():
        text = note_var.get().strip()
        if text and session is not None:
            session.note(text)
            note_var.set("")

    start_btn.config(command=on_start)
    pause_btn.config(command=on_pause)
    stop_btn.config(command=on_stop)
    note_btn.config(command=on_note)

    def on_close():
        if session is not None and session.stop_time is None and session.start_time is not None:
            if not messagebox.askyesno("Κλείσιμο", "Η δοκιμή τρέχει. Να αποθηκευτεί και να κλείσει;"):
                return
            worker_stop.set()
            session.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    tick()
    root.mainloop()


if __name__ == "__main__":
    run_gui()
