package lk.ac.nibm.gnlc.data

import android.content.Context
import com.google.gson.Gson
import lk.ac.nibm.gnlc.net.ApiProvider
import lk.ac.nibm.gnlc.net.AssistanceRequest
import lk.ac.nibm.gnlc.net.BypassRequest
import lk.ac.nibm.gnlc.net.ChecklistBundleDto
import lk.ac.nibm.gnlc.net.DisplayedAtRequest
import lk.ac.nibm.gnlc.net.ManualEntryRequest
import lk.ac.nibm.gnlc.net.ScanPayload
import lk.ac.nibm.gnlc.util.Prefs
import lk.ac.nibm.gnlc.util.Time
import lk.ac.nibm.gnlc.work.SyncScheduler
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.util.UUID

/**
 * The one place a scan is recorded, and the one place the ordering rule is
 * enforced: **Room first, network second, always.**
 *
 * Every screen goes through here. There is no path that posts a scan without
 * writing it down first, because the guarantee that no garment is lost is not
 * a property of the server or of WorkManager — it is a property of this
 * ordering, and it only holds if nothing bypasses it.
 */
class ScanRepository(private val context: Context) {

    private val db = Db.get(context)
    private val prefs = Prefs(context)
    private val gson = Gson()

    // ============================================================= capture ==

    /**
     * Result of a capture, as the screen needs it.
     *
     * [queued] true means the scan is on disk and no verdict exists yet. The
     * screen must say so plainly: the garment has not been checked. Showing a
     * tick here would be a lie with consequences, because the operator would
     * pack it as verified.
     */
    data class Outcome(
        val localRowId: Long,
        val queued: Boolean,
        val verdict: String?,
        val outcome: String?,
        val size: String?,
        val message: String,
        val serverScanId: Long?,
        val recognitionResultId: Long?,
        val engineMs: Int?,
        val endToEndMs: Long?,
    )

    /**
     * Record a scan and try to send it.
     *
     * The insert happens before the POST and is never conditional on it. If
     * the process dies on the next line, the row is still there and
     * [lk.ac.nibm.gnlc.work.SyncWorker] will send it.
     */
    suspend fun capture(
        stage: String,
        checklistId: Int?,
        cartonId: Int?,
        imageFile: File?,
        hangtagBarcode: String?,
        hangtagSizeRaw: String?,
        scannedStyle: String?,
        scannedColour: String?,
        capturedAt: Long,
    ): Outcome {

        val localId = UUID.randomUUID().toString()

        // ---- 1. disk, first --------------------------------------------
        val rowId = db.scans().insert(
            ScanEntity(
                localId = localId,
                deviceId = prefs.deviceId,
                operatorId = prefs.operatorId,
                checklistId = checklistId,
                cartonId = cartonId,
                stage = stage,
                hangtagBarcode = hangtagBarcode,
                hangtagSizeRaw = hangtagSizeRaw,
                scannedStyle = scannedStyle,
                scannedColour = scannedColour,
                imagePath = imageFile?.absolutePath,
                capturedAt = capturedAt,
                syncState = SyncState.QUEUED,
                queuedAt = Time.nowMillis(),
            )
        )

        // ---- 2. network, second ----------------------------------------
        val sent = trySend(rowId)
        if (sent == null) {
            // Nothing is lost; it is queued. WorkManager will retry, and it
            // survives the app being killed and the phone rebooting.
            SyncScheduler.requestSyncNow(context)
            return Outcome(
                localRowId = rowId, queued = true, verdict = null,
                outcome = null, size = null,
                message = "Queued — no verdict yet. This garment has not been checked.",
                serverScanId = null, recognitionResultId = null,
                engineMs = null, endToEndMs = null,
            )
        }

        val row = db.scans().byId(rowId)
        return Outcome(
            localRowId = rowId,
            queued = false,
            verdict = sent.verdict,
            outcome = sent.outcome,
            size = sent.canonicalSize,
            message = sent.message,
            serverScanId = sent.scanEventId,
            recognitionResultId = sent.recognitionResultId,
            engineMs = sent.engineMs,
            endToEndMs = row?.respondedAt?.let { it - capturedAt },
        )
    }

    /**
     * Send one queued scan. Returns null on any failure, having recorded why.
     *
     * The `localId` already on the row is reused rather than regenerated, so a
     * POST that actually succeeded before the connection dropped is
     * deduplicated by the server on `(device_id, local_id)` instead of
     * becoming a second counted garment.
     */
    suspend fun trySend(rowId: Long): lk.ac.nibm.gnlc.net.ScanResponseDto? {
        val row = db.scans().byId(rowId) ?: return null
        val api = ApiProvider.get(context) ?: run {
            db.scans().fail(rowId, "Server address is not set")
            return null
        }

        val payload = ScanPayload(
            localId = row.localId,
            deviceId = row.deviceId,
            operatorId = row.operatorId,
            checklistId = row.checklistId,
            cartonId = row.cartonId,
            stage = row.stage,
            hangtagBarcode = row.hangtagBarcode,
            hangtagSizeRaw = row.hangtagSizeRaw,
            scannedStyle = row.scannedStyle,
            scannedColour = row.scannedColour,
            capturedAt = Time.iso(row.capturedAt),
            queuedAt = Time.iso(row.queuedAt),
        )

        val imagePart = row.imagePath
            ?.let(::File)
            ?.takeIf { it.exists() && it.length() > 0 }
            ?.let { file ->
                MultipartBody.Part.createFormData(
                    "image", file.name,
                    file.asRequestBody("image/jpeg".toMediaType()),
                )
            }

        db.scans().setState(rowId, SyncState.SENDING)

        return try {
            val response = api.scan(
                payload = gson.toJson(payload)
                    .toRequestBody("application/json".toMediaType()),
                image = imagePart,
            )
            val now = Time.nowMillis()
            db.scans().acknowledge(
                id = rowId,
                serverId = response.scanEventId,
                recogId = response.recognitionResultId,
                verdict = response.verdict,
                outcome = response.outcome,
                size = response.canonicalSize,
                engineMs = response.engineMs,
                message = response.message,
                respondedAt = now,
                syncedAt = now,
            )
            // The image has done its job. Keeping it would fill the phone
            // after a few thousand garments; the host keeps the ones that
            // answer a question (no-read, wrong-read, mismatch).
            row.imagePath?.let { runCatching { File(it).delete() } }
            db.scans().clearImagePath(rowId)
            response
        } catch (e: Exception) {
            db.scans().fail(rowId, e.describe())
            null
        }
    }

    // ======================================================== manual entry ==

    /**
     * The fallback after a failed recognition, required by O1: no garment may
     * be blocked indefinitely.
     *
     * [recognitionResultId] is the id of the recognition that actually failed.
     * It is a non-null parameter because this function is only reachable from
     * a no-read result, and a nullable one would be an invitation to call it
     * from somewhere else with null — which is what [bypass] is for.
     */
    suspend fun manualFallback(
        serverScanId: Long,
        recognitionResultId: Long,
        enteredSize: String,
        reason: String?,
        stage: String,
        checklistId: Int?,
        cartonId: Int?,
    ): Boolean {
        val id = db.manualEntries().insert(
            ManualEntryEntity(
                localId = UUID.randomUUID().toString(),
                deviceId = prefs.deviceId,
                operatorId = prefs.operatorId,
                checklistId = checklistId,
                cartonId = cartonId,
                stage = stage,
                enteredSize = enteredSize,
                reason = reason,
                capturedAt = Time.nowMillis(),
                serverScanId = serverScanId,
                recognitionResultId = recognitionResultId,
            )
        )
        return sendManual(id)
    }

    /**
     * A size entered with no scan attempted — a bypass of the control.
     *
     * ⭐ This is the DP3 measurement in §5.8. It goes to its own endpoint,
     * where the server writes the null foreign key itself, so no amount of
     * carelessness in this client can make a bypass look like a legitimate
     * fallback. A high bypass rate is a finding about the design, not a fault
     * to be smoothed over: DP3 predicts that a compliant path slower than the
     * bypass gets bypassed, and the artefact has to be able to report that
     * against itself.
     */
    suspend fun bypass(
        enteredSize: String,
        reason: String?,
        stage: String,
        checklistId: Int?,
        cartonId: Int?,
    ): Boolean {
        val id = db.manualEntries().insert(
            ManualEntryEntity(
                localId = UUID.randomUUID().toString(),
                deviceId = prefs.deviceId,
                operatorId = prefs.operatorId,
                checklistId = checklistId,
                cartonId = cartonId,
                stage = stage,
                enteredSize = enteredSize,
                reason = reason,
                capturedAt = Time.nowMillis(),
                serverScanId = null,
                recognitionResultId = null,   // stays null, by design
            )
        )
        return sendManual(id)
    }

    /** Sends one manual entry down whichever of the two paths it belongs to. */
    suspend fun sendManual(rowId: Long): Boolean {
        // Looked up directly. Scanning the pending list for it would quietly
        // start returning "already sent" once the queue grew past the limit.
        val row = db.manualEntries().byId(rowId) ?: return true
        if (row.sent) return true
        val api = ApiProvider.get(context) ?: run {
            db.manualEntries().fail(rowId, "Server address is not set")
            SyncScheduler.requestSyncNow(context)
            return false
        }

        return try {
            if (row.recognitionResultId != null && row.serverScanId != null) {
                api.manualEntry(
                    ManualEntryRequest(
                        scanEventId = row.serverScanId,
                        enteredSize = row.enteredSize,
                        recognitionResultId = row.recognitionResultId,
                        reason = row.reason,
                    )
                )
            } else {
                api.bypass(
                    BypassRequest(
                        localId = row.localId,
                        deviceId = row.deviceId,
                        operatorId = row.operatorId,
                        checklistId = row.checklistId,
                        cartonId = row.cartonId,
                        stage = row.stage,
                        enteredSize = row.enteredSize,
                        capturedAt = Time.iso(row.capturedAt),
                        reason = row.reason,
                    )
                )
            }
            db.manualEntries().markSent(rowId)
            true
        } catch (e: Exception) {
            db.manualEntries().fail(rowId, e.describe())
            SyncScheduler.requestSyncNow(context)
            false
        }
    }

    // ================================================ the third timestamp ==

    /**
     * Record when the verdict actually appeared, then tell the host.
     *
     * §5.7 reports what the operator experiences — capture to screen — not the
     * engine's 758 ms. Only the device knows this moment, and it cannot be
     * reconstructed afterwards, so it is written locally first and sent
     * separately. A failed send leaves the local value intact for the sync
     * worker to carry later.
     */
    suspend fun markDisplayed(rowId: Long, millis: Long = Time.nowMillis()) {
        db.scans().setDisplayedAt(rowId, millis)
        val row = db.scans().byId(rowId) ?: return
        val serverId = row.serverScanId ?: return
        val api = ApiProvider.get(context) ?: return
        runCatching {
            api.markDisplayed(serverId, DisplayedAtRequest(Time.iso(millis)))
            db.scans().markDisplayedSent(rowId)
        }
    }

    // ============================================================== bundle ==

    /**
     * Fetch the offline bundle and cache it.
     *
     * Returns false when the host is unreachable AND nothing is cached, which
     * is the only case where the operator genuinely cannot proceed. If a
     * bundle is already cached, a failed refresh is not an error — working
     * from the cached copy is the entire point of having one.
     */
    suspend fun loadBundle(checklistId: Int): Boolean {
        val api = ApiProvider.get(context)
        if (api != null) {
            val fetched = runCatching { api.bundle(checklistId) }.getOrNull()
            if (fetched != null) {
                cacheBundle(fetched)
                return true
            }
        }
        return db.cache().checklist(checklistId) != null
    }

    private suspend fun cacheBundle(b: ChecklistBundleDto) {
        db.cache().putChecklist(
            ChecklistCacheEntity(
                checklistId = b.checklistId,
                shipmentId = b.shipmentId,
                shipmentReference = b.shipmentReference,
                stage = b.stage,
                styleNo = b.styleNo,
                colour = b.colour,
                buyerId = b.buyerId,
                buyerCode = b.buyerCode,
                validSizes = b.validSizes.joinToString(","),
                fetchedAt = Time.nowMillis(),
            )
        )
        // Replaced wholesale rather than merged. A barcode removed on the host
        // — a code retired or corrected — must disappear from the phone, and a
        // merge would leave the stale row resolving to the old size.
        db.cache().clearBarcodes(b.checklistId)
        db.cache().putBarcodes(
            b.barcodes.map {
                BarcodeCacheEntity(
                    checklistId = b.checklistId, barcode = it.barcode.uppercase(),
                    size = it.size, buyerNotation = it.buyerNotation,
                    styleNo = it.styleNo, colour = it.colour, sku = it.sku,
                )
            }
        )
        db.cache().putSizeMap(
            b.sizeMap.map { (notation, canonical) ->
                SizeMapCacheEntity(b.checklistId, notation.uppercase(), canonical)
            }
        )
        db.cache().putExpectations(
            b.items.mapIndexed { i, it ->
                ExpectationCacheEntity(
                    checklistId = b.checklistId, size = it.size,
                    expectedQty = it.expectedQty, verifiedQty = it.verifiedQty,
                    sortOrder = i,
                )
            }
        )
    }

    suspend fun cachedChecklist(id: Int) = db.cache().checklist(id)
    suspend fun cachedExpectations(id: Int) = db.cache().expectations(id)
    suspend fun cachedBarcodeCount(id: Int) = db.cache().barcodeCount(id)

    /**
     * Resolve a scanned hangtag from the cache.
     *
     * Returns null when the barcode is not in the map. Null is not a mismatch
     * and must never be presented as one: an unknown barcode is a data gap,
     * and reporting it as a size error would be a false positive counted
     * against O2 — and would send the operator to pull a garment that is
     * probably fine.
     */
    suspend fun resolveHangtag(checklistId: Int, barcode: String) =
        db.cache().barcode(checklistId, barcode.trim().uppercase())

    suspend fun canonicalFor(checklistId: Int, notation: String) =
        db.cache().canonicalFor(checklistId, notation.trim().uppercase())

    // =========================================================== the queue ==

    suspend fun pendingCount(): Int =
        db.scans().pendingCount() + db.manualEntries().pendingCount()

    suspend fun recent(limit: Int = 100) = db.scans().recent(limit)

    suspend fun lateMismatches() = db.scans().unshownLateMismatches()

    suspend fun markLateAlertShown(id: Long) = db.scans().markLateAlertShown(id)

    suspend fun countInCarton(cartonId: Int) = db.scans().countInCarton(cartonId)

    suspend fun countForChecklist(checklistId: Int) =
        db.scans().countForChecklist(checklistId)

    suspend fun countForSize(checklistId: Int, size: String) =
        db.scans().countForSize(checklistId, size)

    suspend fun countSizeInCarton(cartonId: Int, size: String) =
        db.scans().countSizeInCarton(cartonId, size)

    // ========================================================== assistance ==

    /**
     * The operator asked for help. Counted in §5.6.
     *
     * Dropped rather than queued when the host is unreachable. It is a
     * usability signal, not a verification record, and a queue of them would
     * compete with the scans for the connection the moment it comes back —
     * scans are what O3 cannot afford to lose.
     */
    suspend fun assistance(stage: String, shipmentId: Int?, cause: String) {
        val api = ApiProvider.get(context) ?: return
        runCatching {
            api.assistance(
                AssistanceRequest(
                    operatorId = prefs.operatorId, shipmentId = shipmentId,
                    stage = stage, cause = cause, note = null,
                )
            )
        }
    }
}

/** A short, honest error string for the queue screen. */
internal fun Exception.describe(): String = when (this) {
    is java.net.SocketTimeoutException -> "Timed out"
    is java.net.UnknownHostException -> "Host not found"
    is java.net.ConnectException -> "Cannot reach the server"
    // 403 is the shipment-assignment check on the host. It is named rather
    // than left as "Server said 403" because it is the one server error with
    // a fix the operator can ask for: the supervisor ticks them on that
    // shipment and the queued scan then goes through on the next retry. The
    // row is kept and retried like any other, not abandoned — abandoning it
    // would drop a garment from the count.
    is retrofit2.HttpException -> when (code()) {
        403 -> "Not assigned to this shipment — ask your supervisor"
        401 -> "Signed out on the host — sign in again"
        else -> "Server said ${code()}"
    }
    else -> this::class.java.simpleName + (message?.let { ": $it" } ?: "")
}
