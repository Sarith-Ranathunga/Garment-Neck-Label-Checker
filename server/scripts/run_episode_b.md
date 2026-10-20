# Running Episode B

Two modes. Run **both** — they answer different questions, and the chapter
reports both.

---

## Mode 1 · sampled profile (fast, minutes)

Episode A's measured outcome rates are sampled. This is the dry run already
reported in §5.5.

```powershell
docker compose exec api python scripts/episode_b.py 1 --ocr-profile none --out /var/lib/gnlc/exports/epB_none
docker compose exec api python scripts/episode_b.py 1 --ocr-profile open --out /var/lib/gnlc/exports/epB_open
docker compose exec api python scripts/episode_b.py 1 --ocr-profile film --out /var/lib/gnlc/exports/epB_film
docker compose exec api python scripts/episode_b.py 1 --ocr-profile open --no-size-map --out /var/lib/gnlc/exports/epB_nomap
```

`none` must return 100% / 0%. If it does not, the comparison logic is broken
and nothing else in the episode means anything.

---

## Mode 2 · real images (slow, hours on CPU)

**This is the one to report.** EasyOCR runs over the actual Episode A
photographs, so the recognition errors are *observed* rather than modelled.

### What you need

| | |
|---|---|
| The 160 photographs | `C:\Users\Sarith\Downloads\ocr_test` |
| Ground truth | a JSON file, `G01` → size, from sheet 2 of `OCR_Test_Recording_Sheet_FILLED_V03.xlsx` |

The ground-truth file is in the repo as `server/scripts/garment_truth.json` —
beside the script, because the Docker build context is `./server` and anything
outside it cannot be copied into the image. It was
generated from the garment register, which recorded the true size **before any
recognition was run** and had it verified by a second person — that ordering is
what stops the ground truth being contaminated by the engine's own output.

### Run it

```powershell
# make the corpus visible inside the container
docker compose run --rm -v "C:\Users\Sarith\Downloads\ocr_test:/corpus:ro" api `
  python scripts/episode_b.py 1 `
    --ocr-profile real --condition A `
    --images /corpus `
    --truth scripts/garment_truth.json `
    --out /var/lib/gnlc/exports/epB_real_A

# and again for the film condition
docker compose run --rm -v "C:\Users\Sarith\Downloads\ocr_test:/corpus:ro" api `
  python scripts/episode_b.py 1 `
    --ocr-profile real --condition B `
    --images /corpus `
    --truth scripts/garment_truth.json `
    --out /var/lib/gnlc/exports/epB_real_B
```

Add `--gpu` if the host has a usable GPU. On CPU expect roughly a minute per
image for the first pass — 80 images per condition. The sweep itself is then
instant, because every image is recognised **once** and cached.

### What it writes

| File | Contents |
|---|---|
| `epB_real_A.csv` / `.json` | the sweep, one row per injection rate |
| `epB_real_A_images.csv` | **one row per photograph** — raw OCR text, parsed size, outcome, rotation used, time |

The per-image file is the important one. Every figure in the sweep traces back
to a specific photograph, so a reader can check any of them.

---

## ⚠️ Expect the real run to disagree with Episode A, and report why

Episode A recorded **93.75% correct / 2.50% wrong / 3.75% no read** (open).
The real run will show **more no reads and fewer correct**, and that is not a
regression — it is a different, stricter parser.

Episode A used a loose parser that picked a size whenever one appeared.
The artefact refuses to guess when two valid sizes appear as whole tokens.

A worked example, from `G01_A_000.jpeg`:

```
raw OCR  :  CORE B A S / C $ L IN LANKA SRI MaDE
candidates: ['L', 'S']          <- two valid sizes, as whole tokens
verdict   : NO READ
```

The garment is an **L**. The label's brand text is *CORE BASIC*, which OCR
fragments into single characters — and the `S` from `BASIC` is indistinguishable
from the size code `S`. Episode A's parser took `L` and was right by luck.
This one declines, and the garment goes to manual entry.

**That trade is the artefact's central design position, and here it has a
measured cost.** §4.6 already states that ambiguity resolving to a no read
"costs a little read rate and buys safety". The real run is what turns that
sentence from an assertion into a number, and it gives §5.5 a concrete
mechanism to name: *single-character fragments of surrounding brand text
collide with single-character size codes*.

Report both figures and the reason for the gap. A dissertation that quietly
shows 93.75% from Episode A and a lower rate here, without explaining the
difference, invites exactly the question it cannot then answer.

The harness prints the corpus's own measured profile at the top of the run, so
the comparison is in front of you rather than needing to be reconstructed.
