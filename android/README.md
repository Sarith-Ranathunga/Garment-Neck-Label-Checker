# GNLC Android client

The operator's half of the artefact. Kotlin, `lk.ac.nibm.gnlc`, min SDK 26,
built with XML layouts and ViewBinding.

This was written without an Android SDK available, so it has **not been
compiled**. Everything that could be checked statically has been
(`python3 tools/verify.py`, below), but the first real build is on your
machine. Expect one or two small fixes; send me the errors and I will fix them.

---

## Build it

1. **Open the folder `android/`** in Android Studio — not `gnlc/`. Android
   Studio needs the folder containing `settings.gradle.kts`.
2. Let it sync. The first sync downloads Gradle 8.9, the Android Gradle Plugin
   and the libraries; on a normal connection this takes a few minutes.
3. `Build ▸ Make Project`, or in a terminal inside `android/`:

   ```
   ./gradlew assembleDebug
   ```

4. Plug in the Galaxy A21s with USB debugging on, then `Run ▸ Run 'app'`.

If the sync complains about the JDK, set **File ▸ Settings ▸ Build, Execution,
Deployment ▸ Build Tools ▸ Gradle ▸ Gradle JDK** to the bundled JDK 17 or to
your JDK 21. Both work; the project targets 17 bytecode deliberately.

### Versions are pinned

`gradle/libs.versions.toml` holds every version. If Android Studio offers to
upgrade something, decline until after submission. A set of versions that work
together is worth more right now than newer ones.

---

## First run, on the phone

1. Open the app. It shows **Settings** first because no server address is set.
2. Enter the host PC's address — `http://192.168.1.50:8000`, with the real IP.
   Typing `192.168.1.50:8000` without the scheme also works.
3. Tap **Test connection**. It must say *Connected*. If it does not, the
   message tells you what to check; work through it before going further,
   because nothing else in the app will work until this does.
4. Tap **Save**, then pick your name from the operator list.
5. Pick a checklist. The supervisor opens checklists on the host; if the list
   is empty, none are open.

The address is stored per phone, so each handset is set up once.

---

## What each screen is for

| Screen | Stage | What happens |
|---|---|---|
| Sign in | — | Pick the operator. Needed because O4 compares before and after per operator |
| Settings | — | Server address, device ID, connection test |
| Checklist | — | Choose the open checklist. The stage comes from it, not from the operator |
| Capture | S1 | Photograph the neck label. The size is **established** here — there is no hangtag yet, so nothing to verify against |
| Capture | S5 | Scan the hangtag, then photograph the label. The two are compared |
| Manual entry | any | Enter a size from the shipment's allocated sizes. Two paths — see below |
| Carton | S6 | Open a carton, scan garments into it with a running count, close it |
| Queue | — | What this phone is still holding, and why |

### Screenshots for §4.5

Take them on the A21s, not an emulator, and pair them two to a figure:

- `Figure 4.7` — S1 capture + result
- `Figure 4.8` — S5 hangtag prompt + mismatch alert
- `Figure 4.9` — no-read result + manual fallback
- `Figure 4.10` — carton with running count + close confirmation
- `Figure 4.11` — the queue with scans pending

---

## The four rules this client follows

These are not style choices. Each one, if broken, produces a figure in
Chapter 5 that looks valid and is not.

### 1 · Room before the network, always

`ScanRepository.capture()` inserts the scan, then posts it. Never the other
way round. The phone can lose power between the two and the scan still
survives, because the row exists and `SyncWorker` will find it. A client that
posted first and recorded the answer would lose every scan taken during an
outage, and O3's reconciliation would quietly under-count.

`tools/verify.py` checks this ordering on every run.

### 2 · A queued scan is never shown as a success

It says **QUEUED — this garment has not been checked yet**, in its own colour.
Not a tick, not an error. The check still runs when the queue drains, so the
error is still caught — but possibly after the carton is closed, which is why
`queuedAt` and `syncedAt` are both kept and why a late mismatch raises a
dialog telling the operator to go and find the garment.

### 3 · `recognition_result_id` is never invented

Two different things reach the manual-entry screen:

- **Fallback** — recognition ran and returned no-read. The id of *that*
  recognition is sent to `/api/manual-entry`. This is the O1 escape hatch:
  no garment can be blocked indefinitely.
- **Bypass** — the operator skipped the scan. This goes to `/api/bypass`,
  where the **server** writes the null foreign key.

They are kept apart three times over: a different flag in the intent, a
different field in the local row, a different endpoint on the host. The
proportion of bypasses is the DP3 finding in §5.8, and a single careless line
here would make every bypass look like a legitimate fallback — §5.8 would then
report a number nobody could challenge.

The bypass button is deliberately **visible**. Hiding it would push operators
to work around the app entirely, and the behaviour DP3 predicts would become
unmeasurable rather than rare.

### 4 · `local_id` is generated once and reused on every retry

The server deduplicates on `(device_id, local_id)`. A fresh UUID on retry
would turn one timed-out scan into two counted garments and corrupt the
reconciliation. `trySend()` reuses the id already on the row; the verifier
checks that it does not call `UUID.randomUUID()`.

---

## Three timestamps

| Timestamp | Set where | Why it cannot come from anywhere else |
|---|---|---|
| `captured_at` | phone, before the shutter | The figure §5.7 reports starts when the operator acts |
| `responded_at` | host, on reply | — |
| `displayed_at` | phone, when the verdict is on screen | Only the device knows this moment, and it cannot be reconstructed afterwards |

`displayed_at` is sent in its own call, so it can fail independently.
`displayedAtSent` tracks whether it got through: without that flag, verdicts
shown during an outage would be missing from the latency figure, which would
then describe only the scans taken while the network was healthy — the fast
ones. A biased sample, not a smaller one.

---

## Offline behaviour

What keeps working with the WiFi down, and what does not:

| Works offline | Why |
|---|---|
| Photographing labels | Queued to Room, sent later |
| Reading hangtag barcodes | ML Kit is on-device |
| Resolving a hangtag to a size | The barcode map is cached per checklist |
| The size keypad | The allocated size list is cached |
| The running carton count | Counted from the phone's own rows |

| Needs the host | Why |
|---|---|
| Choosing a checklist first time | Nothing cached for it yet |
| Opening a carton | The carton id every scan is filed against comes from the host |
| Closing a carton | Contents are frozen server-side from the scans |
| Verdicts | Recognition runs on the host |

The barcode map being cached is the part that matters most. A UPC-A hangtag
encodes nothing — not the size, not the style — so an offline phone without
that map could scan a hangtag and learn nothing from it, and S5 would be
impossible rather than merely delayed.

---

## Static checks

```
cd android
python3 tools/verify.py
```

It checks: every layout parses; every `@string`/`@color`/`@style` reference
resolves; every `b.someView` exists as an id in that binding's layout; every
Room `@Query` names real columns and real bind parameters; every `libs.*`
alias is in the version catalogue; every manifest activity exists on disk and
every activity on disk is in the manifest; every endpoint `Api.kt` calls
exists in the FastAPI service; and the four rules above.

It is not a compiler. It cannot see a type error — and the first real build
proved it, so the checks it does make were grown from actual failures:

| Found by | Problem | Now checked by |
|---|---|---|
| The checker, first run | `--` inside an XML comment, in ten files. Illegal in XML; `aapt2` rejects it | `check_xml_resource_refs` |
| **The first Gradle build** | Four styles with dotted names and no `parent`. `Gnlc.Label` implicitly inherits from `Gnlc`, which does not exist → `resource style/Gnlc not found` | `check_implicit_style_parents` |
| Reading the build log | `BuildConfig.DEBUG` in `ApiProvider`, with no `buildConfig = true`. AGP 8.0 stopped generating `BuildConfig` by default, so this fails at the Kotlin step — *after* resource linking, which means it hides behind any resource error | `check_buildconfig_optin` |

Both build failures share a shape worth noting: the error message names the
thing that is missing, not the line that asked for it. `resource style/Gnlc
not found` appears four times, identically, and never mentions `Gnlc.Label`.
Each new check reproduces its own bug when the fix is reverted, which is how
they were verified.

---

## Known limitations, declared

| Limitation | Where it is recorded |
|---|---|
| No password on sign-in | §4.9; §6.4 lists authentication as further work |
| Cleartext HTTP on the factory LAN | `res/xml/network_security_config.xml`; §6.4 lists TLS |
| Carton weights typed, not measured | §4.8 field-map gap, reported in O3 |
| Release build is not minified | Keeps Room and Retrofit reflection working |
| Assistance events are dropped, not queued, when offline | They are a usability signal, not a verification record, and must not compete with scans for the connection |
