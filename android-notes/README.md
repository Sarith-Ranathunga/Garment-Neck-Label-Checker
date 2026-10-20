# Android client — build notes

**Superseded.** These were the decisions fixed before the client was written.
The client now exists, in [`../android/`](../android/README.md), and that
README is the one to read: it covers building it, running it on the phone,
and which screenshots to take for §4.5.

This file is kept because it records *what was decided before any code was
written, and why* — which is the part of a design-science build that a viva
asks about and that finished code no longer shows.

---

## What was decided here, and how it turned out

| Decision | Held? | Note |
|---|---|---|
| Kotlin, `lk.ac.nibm.gnlc`, min SDK 26 | ✅ | |
| CameraX for capture | ✅ | `ImageCapture` with `CAPTURE_MODE_MINIMIZE_LATENCY` |
| ML Kit for the hangtag barcode | ✅ | On-device. Not OCR, so no conflict with the EasyOCR decision |
| Room for the offline queue | ✅ | Load-bearing, not a nicety |
| WorkManager for sync | ✅ | Survives app death and reboot |
| Retrofit + OkHttp | ✅ | |
| **XML + ViewBinding, not Compose** | ✅ | The documented fallback. Fewer version couplings to go wrong on a deadline |
| Write to Room **before** the network call | ✅ | `ScanRepository.capture()`; checked by `tools/verify.py` |
| Never invent `recognition_result_id` | ✅ | Two endpoints, so the client cannot blur them — see below |
| `local_id` + `device_id` on every scan | ✅ | Reused on retry; a test proves one retry is one garment |
| Three timestamps per scan | ✅ | `displayed_at` sent in its own call, with a sent-flag |
| Server address as a setting | ✅ | Plus a **Test connection** button, which was not planned |

### What changed from these notes

Three things turned out differently once the code met the server.

**1 · The bypass needed its own endpoint.** The note said "never invent
`recognition_result_id`" and left the client to send null. That put the
honesty of the §5.8 measurement in the client's hands: one careless line and
every bypass looks like a legitimate fallback. The server now has
`POST /api/bypass`, which can only be called when there was no recognition,
and the **server** writes the null. The client can no longer misreport it even
by accident.

**2 · `/api/scan` had to return `recognition_result_id`.** It did not, which
meant a client reaching manual entry after a no-read had nothing truthful to
point at. Found by writing the client; fixed on the server.

**3 · The barcode map had to be sent to the device.** Not in these notes at
all. A UPC-A hangtag encodes nothing, so an offline phone with no cached map
can scan a hangtag and learn *nothing* from it — S5 becomes impossible rather
than merely delayed. `GET /api/checklist/{id}/bundle` now sends the map, the
allocated size list and the buyer notation map, and the phone caches them
until the checklist is closed.

---

## Still true, and still worth reading

### Queued scans are honest, not silent

A queued scan shows **"queued — verdict pending"**, in its own colour. Not a
tick and not an error.

A queued scan means the operator packed without knowing whether the sizes
matched. The check still runs when the queue syncs — so the error is still
detected — but possibly after the carton is closed. `queued_at` and
`synced_at` exist so §5.5 can report how often that happened instead of
estimating it, and a verdict that comes back late as a mismatch raises a
dialog telling the operator to go and find the garment.

The carton screen refuses to close a carton while scans are still queued, for
the same reason: a carton frozen from an incomplete set of events would show a
shortfall that looks like a packing error and is not.

### The three recognition outcomes keep their own colours

Green match, red mismatch, **amber** no-read. Amber rather than a softer red,
because a no-read is a *safe* outcome — the system declined to guess — and
colouring it like a failure would teach operators to treat the two the same.
That is the distinction the whole artefact exists to preserve (§3.09).

### Test on the actual device

The Galaxy A21s, not an emulator and not a better phone. Episode A was
specified for a worker-class Android device under factory lighting, and
performance on a flagship would not describe what the artefact does.
