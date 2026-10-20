# Garment Neck Label Checker (GNLC)

Neck-label verification for Sri Lankan SME apparel packing departments.

**S. J. Ranathunga** · COBSCCOMP242P-011 · NIBM School of Computing & Engineering
Supervisor: Mr. Niranga Dharmarathne

---

## What this is

A **job aid** — an established intervention class in visual inspection, defined as anything used to assist the operator with the task (Charles et al., 2015, p. 90). It sits on top of the existing packing process and removes no stage from it.

An Android phone reads the garment's sewn-in neck label; a service on an existing supervisor PC recognises the size and compares it against the hangtag. If any part of the system is unavailable, the manual process continues exactly as it does today.

**The binding constraint the whole design answers:** the only identifier that tells the truth about a garment is not machine-readable by conventional means, and no readable one may be added.

---

## Architecture

```
Android phone          existing WiFi          Supervisor PC
(the only scanner) ───────────────────▶  FastAPI + EasyOCR
                   ◀───────────────────  PostgreSQL
  barcode decoded                          supervisor web interface
  on the device                            (packing-flow monitoring only)
                                                    │
                                           encrypted USB drive
                                           backup every 15 minutes
```

**Barcode on the device, OCR on the server.** A barcode is designed to be machine-read and a phone decodes it instantly offline. The neck label is ordinary printed text and needs EasyOCR, which needs PyTorch, which will not run on a worker-class Android phone. That is the only thing that has to leave the device.

**No internet.** Everything stays inside the factory.

---

## Why EasyOCR

Chosen on this study's own measurement, not on a claim from the literature. Episode A compared two engines under identical conditions — 20 garments × 2 conditions × 4 rotations = 160 images, 320 recognition events:

| Condition | Tesseract | **EasyOCR** |
|---|---|---|
| Label directly accessible | 88.75% correct | **93.75% correct** |
| Through packaging film | 86.25% correct | **90.00% correct** |
| Mean time | 783 / 761 ms | 758 / 766 ms |

More accurate in both conditions at equivalent speed.

**Correct reads by rotation** — why `ROTATIONS` exists:

| Rotation | Accessible | Through film |
|---|---|---|
| 0° | 95.0% | 95.0% |
| 90° | 95.0% | 90.0% |
| 180° | 80.0% | 90.0% |
| 270° | 85.0% | **70.0%** |

At 0° the two conditions are identical. **Orientation, not the packaging film, is the limiting factor** — which was not the expectation going in. A worker holding a garment has no fixed orientation, so the four-rotation retry is a response to a measured failure mode.

---

## Running it

```bash
cp .env.example .env          # set GNLC_DB_PASSWORD and GNLC_USB_PATH
docker compose up -d
curl http://localhost:8000/health
```

The schema loads automatically on first start. API documentation is generated at `/docs`.

### Two things that will otherwise waste an afternoon

**Give the PC a static IP.** Addresses handed out by DHCP change. When the PC's address changes, every phone loses the service. Set a static IP or ask IT to reserve one, and put the address in the app's settings screen so it can be changed without rebuilding the app.

**Open the inbound port in Windows Firewall.** Windows blocks it by default. The PC will report the service running and the phones will report it unreachable.

---

## Layout

```
db/schema.sql               23 tables, 3 views. Verified against PostgreSQL 16
db/seed_demo.sql            demo data covering every outcome
server/app/recognition.py   ⭐ the research contribution
server/app/main.py          API
server/scripts/backup.py    15-minute USB backup
server/scripts/export_shipment.py   per-shipment Excel workbook
server/tests/               20 tests, no OCR model required
diagrams/                   Figures 4.1 – 4.6
```

### `recognition.py` is the part that matters

Everything else is ordinary software. That module holds the recognition model and the verification algorithm — the two things Chapter 4 §4.6 asks for.

```bash
python server/tests/test_recognition.py        # 20 passed
```

The tests inject a fake recogniser, so the constrained parse and the notation resolution run without loading a neural network.

---

## Four design decisions worth knowing

### 1 · Whole-token matching, never substring

A substring search produces **wrong reads** — the dangerous outcome — in two ways that both occur on real labels:

- `XXL` is a substring of `XXXL`. A 3XL garment reads as 2XL.
- Single-character sizes sit inside ordinary words. `S` is inside `SRI`, `M` inside `MADE`, `L` inside `LANKA`. Every label reading *MADE IN SRI LANKA* would match S, M and L at once.

Both were caught by the test suite during the build. Splitting on non-alphanumeric characters and comparing whole tokens removes them.

### 2 · Ambiguity is a failure, not a coin toss

If two valid sizes both appear as whole tokens the read is not trustworthy — OCR noise fragments words, so `SRI` can arrive as `S` beside a genuine `L`. The next rotation is tried instead; different orientation, different noise. If every rotation is ambiguous the result is **NO READ** and the garment goes to manual entry.

That costs a little read rate and buys safety, which is the trade this design makes everywhere.

### 3 · Three outcomes, never combined

| Outcome | Meaning | |
|---|---|---|
| Correct | the right size is returned | |
| **Wrong read** | a size is returned, but the wrong one | 🔴 dangerous — creates false confidence |
| No read | nothing is returned, manual entry follows | 🟡 safe |

A single "accuracy" figure would conceal exactly the distinction the artefact exists to address.

### 4 · One nullable column measures DP3

`exception_event.recognition_result_id` is nullable **on purpose**:

- **NOT NULL** — recognition ran and returned no read. A legitimate fallback.
- **NULL** — a manual entry with no failed recognition behind it. A **bypass** of the control.

DP3 holds that a compliant path slower than its alternative will be bypassed; Charles et al. (2015, p. 92) observed inspectors deviating from a prescribed procedure for exactly that reason. Until now the risk was cited and never measured. This measures it, and it costs nothing.

`v_bypass` reports the rate per operator.

---

## Backup and recovery

Every 15 minutes, to an **encrypted** USB drive.

| Rule | Why |
|---|---|
| Write to a temp name, then rename | A rename is atomic. A crash mid-dump leaves the previous good backup untouched |
| Keep generations | If a bug corrupts a table at 11:00 and the only backup is 11:15, the corruption has been preserved |
| Log every run | `backup_log`, including `verified_at` from the weekly restore test |
| Never fail silently | Failures are written to the log with their error text |

⚠️ **The drive must be encrypted.** It holds buyer names, purchase order numbers, quantities and prices, and Chapter 3 §3.12 commits this research to treating those as confidential. BitLocker To Go on Windows; LUKS on Linux.

### Restore checklist — test this once before Episode C

```
1  Install Docker on the replacement PC
2  Clone the repo, run docker compose up -d
3  Restore the newest dump from the USB drive
4  Set the SAME static IP as the old machine     ← see below
5  Open the firewall port
6  curl /health
7  Have one phone scan a test garment
8  Tell workers to resume
```

**Step 4 is the one people forget.** Same IP, and the phones need no change at all. Different IP, and every phone must be edited by hand — mid-shift, with workers waiting.

| | |
|---|---|
| Server data lost | at most 15 minutes |
| **Scans lost** | **none** — the phones hold their own queue |
| Time to recover | about 1 hour, if the procedure has been tested |
| Does the factory stop? | **no** — the manual process continues |

---

## Per-shipment Excel export

```bash
python server/scripts/export_shipment.py <shipment_id>
```

Eight sheets, identical for every shipment: `Shipment` · `Scans` · `Mismatches` · `Exceptions` · `Cartons` · `PackingList` · `Reconciliation` · `Operators`.

**The host is the only writer.** An `.xlsx` is a single zip archive with no partial write and no merge; if several devices uploaded their own versions, the last would silently replace the rest. Separate sheets do not help, because a sheet lives inside the one file. Making the host the only writer removes the problem rather than managing it.

Operators get a **column** in `Scans`, not a sheet each — the structure then stays identical whatever the shipment, Excel's filter gives per-operator views in two clicks, and `Operators` already carries the summary.

---

## Images

Retained **only** for `wrong_read` and `no_read`.

Those are the images anyone will ask about: when §5.5 reports a wrong read, having the picture is a strong answer. Tens of thousands of images of labels that read correctly answer nothing and fill the disk. Synced to the USB daily rather than every 15 minutes.

---

## Status

| Increment | | |
|---|---|---|
| 1 | Optical size recognition | ✅ algorithm complete and tested |
| 2 | First size segregation | ✅ API and Android client written |
| 3 | Second size segregation | ✅ API and Android client written |
| 4 | Reconciliation, packing list, Android client | ✅ API, views and client written |
| 4 | Supervisor **web** interface | ✅ Built — `/ui`, five pages |

The Android client is in [`android/`](android/README.md) — 19 Kotlin files, 7
screens. It has not been compiled: no Android SDK was reachable from the
environment it was written in, so the first build is in Android Studio. Two
static checkers stand in for what a compiler would catch:

```
cd android
python3 tools/verify.py        # resources, view bindings, manifest, invariants
python3 tools/verify_sql.py    # every Room query, against real SQLite
```

| Tests | | |
|---|---|---|
| `server/tests/` | 79 | recognition algorithm, barcode resolution, every endpoint, the client contract |
| `android/tools/verify.py` | 11 checks | 19 Kotlin files, 9 layouts |
| `android/tools/verify_sql.py` | 32 queries | prepared and executed against SQLite |

Writing the client found two bugs on the server side, both of the kind that
produce a plausible wrong number rather than an error:

- `/api/scan` never returned `recognition_result_id`, so the client had
  nothing truthful to attach a manual fallback to — it would have had to guess
  or misreport, and either corrupts the DP3 measurement in §5.8.
- An **unknown** hangtag barcode was reported as the **wrong garment**
  (`out_of_context`). Those are different findings: one is a real observation
  about the floor, the other is a gap in the barcode table that says nothing
  about the garment. Merging them would have counted false positives against
  O2 and sent operators to pull garments that were almost certainly correct.

## The supervisor interface

`http://<host>:8000/ui` — monitoring and setup only; nothing there scans.

| Page | What it is for |
|---|---|
| Shipments | every shipment, and whether its allocation balances |
| Dashboard | the four-point reconciliation **with its three deltas**, verdicts by stage, the mismatch log, carton state, and queued-verdict delay |
| Checklists | the supervisor opens and closes them; operators only select |
| Setup | purchase order, shipment, and the size allocation that O3 refuses to accept unbalanced |
| Evaluation | the figures Chapter 5 reports — bypass rate, latency, recognition outcomes, barcode resolution paths |

The reconciliation table shows the deltas rather than only the four counts,
because O3's claim is that a divergence is **attributable to the stage where it
occurred** — and four raw numbers leave the reader to do that subtraction.

| Episode | | |
|---|---|---|
| A — recognition feasibility | artificial, formative | ✅ run |
| B — controlled error injection | artificial, summative | ✅ sampled modes run; **real-image mode built** — `scripts/run_episode_b.md` |
| C — before-and-after study | naturalistic, summative | ❌ needs the APK on the floor, and ethics clearance |

### Episode B now runs against the real photographs

`--ocr-profile real` runs EasyOCR over the Episode A corpus instead of sampling
its measured outcome rates, so the recognition errors are **observed rather
than modelled** — and their correlation across one garment's four rotations is
preserved, which a fitted distribution destroys in the direction that flatters
the system.

⚠️ **It will disagree with Episode A's 93.75%, and the reason is a finding.**
Episode A used a loose parser that picked a size whenever one appeared; the
artefact refuses to guess when two valid sizes appear as whole tokens. On
`G01_A_000.jpeg` the OCR returns `CORE B A S / C $ L IN LANKA SRI MaDE` — the
brand text *CORE BASIC* fragments into single characters, the `S` from `BASIC`
collides with the size code `S`, and the parse sees two candidates and declines.
The garment is an L; Episode A's parser took it and was right by luck.

That is the artefact's central trade with a measured cost attached, and §5.5
should name the mechanism: single-character fragments of surrounding brand text
collide with single-character size codes.
