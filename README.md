# SEO Fix Planner

Ένα layer πάνω στα εργαλεία τεχνικού SEO audit (Semrush, Screaming Frog) που μετατρέπει
τη λίστα προβλημάτων σε **σειρά διορθώσεων** για sites WordPress. Η σειρά λαμβάνει υπόψη:

- ποια διόρθωση λύνει πολλά προβλήματα μαζί,
- ποια διόρθωση πρέπει να γίνει πριν από άλλες (εξαρτήσεις),
- πόσο χρόνο θέλει κάθε διόρθωση.

Μέρος της μεταπτυχιακής διπλωματικής εργασίας (Πανεπιστήμιο Πατρών, MSc Digital Marketing).

## Κατάσταση

| Φάση | Περιεχόμενο | Κατάσταση |
|---|---|---|
| 1 | Εισαγωγή export, κατάλογος διορθώσεων v0, έλεγχος καταλόγου | ✅ |
| 2 | Επέκταση καταλόγου, εξαρτήσεις, πηγές | ⏳ |
| 3 | Αλγόριθμος σειράς | ⏳ |
| 4 | Αξιολόγηση έναντι της σειράς του εργαλείου | ⏳ |

## Εγκατάσταση (μία φορά)

Στο τερματικό του VS Code, μέσα στον φάκελο του repo:

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## Χρήση

1. Βάλε το export του Semrush στον φάκελο `data/raw/`.
2. Εισαγωγή:
   ```bash
   python src/ingest.py data/raw/edge.dmsm2026.neuroverse.gr_mega_export_20261004.xlsx
   ```
3. Έλεγχος καταλόγου και κάλυψης:
   ```bash
   python src/catalog_check.py
   ```

Για audit μετά από διόρθωση, δώσε δικό του όνομα:
```bash
python src/ingest.py data/raw/<νέο_export>.xlsx --audit-id after_F01
```

## Δομή

```
catalog/
  fixes.yaml              κατάλογος διορθώσεων (η κύρια συνεισφορά)
  severity_semrush.yaml   σοβαρότητα κάθε issue κατά Semrush
  config.yaml             βάρη και ρυθμίσεις (προσωρινά)
data/
  raw/                    exports από τα εργαλεία
  processed/              CSV και βάση SQLite (παράγονται αυτόματα)
docs/
  decisions.md            ημερολόγιο αποφάσεων
src/
  ingest.py               εισαγωγή export
  catalog_check.py        έλεγχος καταλόγου και κάλυψης
```

## Αρχή σχεδιασμού

Η γνώση (ποια διόρθωση λύνει τι, εξαρτήσεις, χρόνοι, βάρη) ζει στα αρχεία του `catalog/`,
όχι στον κώδικα. Ό,τι αλλάζει από τη βιβλιογραφία αλλάζει μόνο εκεί, και κάθε αλλαγή
καταγράφεται στο `docs/decisions.md`.
