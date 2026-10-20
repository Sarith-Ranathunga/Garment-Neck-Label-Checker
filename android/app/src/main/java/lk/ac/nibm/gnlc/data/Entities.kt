package lk.ac.nibm.gnlc.data

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

/**
 * Sync states, as the phone sees them.
 *
 * QUEUED is not a failure state. It means the scan is safely on disk and the
 * verdict has not come back yet, which is exactly what the operator is shown:
 * "queued — verdict pending", never a tick and never an error. A tick would
 * tell them the garment had been checked when it had not.
 */
enum class SyncState { QUEUED, SENDING, ACKNOWLEDGED, FAILED }

/**
 * One scan, written to disk before the network is touched.
 *
 * That ordering is the whole offline guarantee. The phone can lose power
 * between the insert and the POST and the scan still survives, because the row
 * exists and WorkManager will find it. A design that posted first and recorded
 * the response would lose every scan taken during an outage, and the
 * reconciliation in O3 would silently under-count.
 */
@Entity(
    tableName = "scan",
    indices = [
        Index(value = ["localId"], unique = true),
        Index(value = ["syncState"]),
        Index(value = ["capturedAt"]),
    ],
)
data class ScanEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,

    /**
     * A UUID generated on the device at capture, and reused on every retry of
     * this same scan. The server deduplicates on `(deviceId, localId)`.
     * Generating a fresh one on retry would turn one timed-out scan into two
     * counted garments.
     */
    val localId: String,
    val deviceId: String,
    val operatorId: Int,

    val checklistId: Int?,
    val cartonId: Int?,
    val stage: String,

    // What the device observed. The hangtag is decoded on the phone by ML
    // Kit; the size notation is resolved from the cached barcode map, which
    // is what lets S5 survive a WiFi outage.
    val hangtagBarcode: String? = null,
    val hangtagSizeRaw: String? = null,
    val scannedStyle: String? = null,
    val scannedColour: String? = null,

    /** Local JPEG path. Deleted once the server has acknowledged the scan. */
    val imagePath: String? = null,

    // The three timestamps §5.7 needs. displayedAt is set when the verdict
    // actually appears on screen, and is the only one the server cannot
    // reconstruct — which is why it is sent back in a separate call.
    val capturedAt: Long,
    val respondedAt: Long? = null,
    val displayedAt: Long? = null,

    /**
     * Whether [displayedAt] has reached the host.
     *
     * It is sent in a second call, after the verdict is on screen, so it can
     * fail independently of the scan. Without this flag a verdict displayed
     * during a WiFi outage would be acknowledged locally and the end-to-end
     * figure §5.7 reports would quietly be missing those rows — biased towards
     * the scans that happened while the network was healthy, which are the
     * fast ones.
     */
    val displayedAtSent: Boolean = false,

    val syncState: SyncState = SyncState.QUEUED,
    val queuedAt: Long = capturedAt,
    val syncedAt: Long? = null,
    val attempts: Int = 0,
    val lastError: String? = null,

    // What came back.
    val serverScanId: Long? = null,
    val recognitionResultId: Long? = null,
    val verdict: String? = null,
    val outcome: String? = null,
    val canonicalSize: String? = null,
    val engineMs: Int? = null,
    val message: String? = null,

    /**
     * True once the operator has been shown a verdict that arrived late — a
     * scan that synced after it was queued and came back a mismatch. The flag
     * stops the same alert being raised at every refresh, while keeping the
     * row's own history intact.
     */
    val lateAlertShown: Boolean = false,
)

/**
 * A manual entry waiting to be sent.
 *
 * [recognitionResultId] is nullable here, and the nullability is the
 * measurement rather than a convenience:
 *
 *  - a value means recognition ran and returned no-read, so this is the
 *    legitimate fallback path required by O1;
 *  - null means the operator entered a size with no scan behind it, which is
 *    a bypass of the control and the direct measure of DP3 in §5.8.
 *
 * The two are sent to different endpoints precisely so this client cannot
 * blur them. See [lk.ac.nibm.gnlc.net.BypassRequest].
 */
@Entity(
    tableName = "manual_entry",
    indices = [Index(value = ["localId"], unique = true), Index(value = ["sent"])],
)
data class ManualEntryEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val localId: String,
    val deviceId: String,
    val operatorId: Int,
    val checklistId: Int?,
    val cartonId: Int?,
    val stage: String,
    val enteredSize: String,
    val reason: String? = null,
    val capturedAt: Long,

    /** The scan this entry belongs to, when there was one. */
    val serverScanId: Long? = null,
    /** Null = bypass. Never filled in to look tidy. */
    val recognitionResultId: Long? = null,

    val sent: Boolean = false,
    val attempts: Int = 0,
    val lastError: String? = null,
)

/** The active checklist, cached so a dropped connection does not lose it. */
@Entity(tableName = "checklist_cache")
data class ChecklistCacheEntity(
    @PrimaryKey val checklistId: Int,
    val shipmentId: Int,
    val shipmentReference: String,
    val stage: String,
    val styleNo: String,
    val colour: String,
    val buyerId: Int,
    val buyerCode: String,
    /** Comma-separated canonical sizes, in packing-list order. */
    val validSizes: String,
    val fetchedAt: Long,
)

/**
 * The barcode map, cached per checklist.
 *
 * Without these rows an offline phone can scan a hangtag and learn nothing
 * from it, because a UPC-A code carries no size. Caching them is the
 * difference between an artefact that works on factory WiFi and one that
 * works in a demonstration.
 */
@Entity(
    tableName = "barcode_cache",
    primaryKeys = ["checklistId", "barcode"],
    indices = [Index(value = ["barcode"])],
)
data class BarcodeCacheEntity(
    val checklistId: Int,
    val barcode: String,
    val size: String,
    val buyerNotation: String?,
    val styleNo: String,
    val colour: String,
    val sku: String?,
)

/** Buyer notation to canonical size. XXL -> 2XL is the row that matters. */
@Entity(tableName = "size_map_cache", primaryKeys = ["checklistId", "notation"])
data class SizeMapCacheEntity(
    val checklistId: Int,
    val notation: String,
    val canonical: String,
)

/** Per-size expectations, for the running count on the operator's screen. */
@Entity(tableName = "expectation_cache", primaryKeys = ["checklistId", "size"])
data class ExpectationCacheEntity(
    val checklistId: Int,
    val size: String,
    val expectedQty: Int,
    val verifiedQty: Int,
    val sortOrder: Int,
)
