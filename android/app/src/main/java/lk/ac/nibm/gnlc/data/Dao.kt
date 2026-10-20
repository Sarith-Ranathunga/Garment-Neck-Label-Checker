package lk.ac.nibm.gnlc.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.TypeConverter
import androidx.room.Upsert

class Converters {
    @TypeConverter fun toSyncState(value: String?): SyncState =
        value?.let { runCatching { SyncState.valueOf(it) }.getOrNull() }
            ?: SyncState.QUEUED

    @TypeConverter fun fromSyncState(state: SyncState?): String =
        (state ?: SyncState.QUEUED).name
}

@Dao
interface ScanDao {

    @Insert
    suspend fun insert(scan: ScanEntity): Long

    @Query("SELECT * FROM scan WHERE id = :id")
    suspend fun byId(id: Long): ScanEntity?

    @Query("SELECT * FROM scan WHERE localId = :localId")
    suspend fun byLocalId(localId: String): ScanEntity?

    /**
     * The send order: oldest capture first.
     *
     * Chronological order is not cosmetic. The carton counts and the four-point
     * reconciliation are built from these events, and a queue that drained
     * newest-first would interleave scans from before and after a carton close.
     *
     * FAILED rows are included. A failure here is almost always the network or
     * a host restart, not bad data, so a row that failed once is retried rather
     * than abandoned — abandoning it would lose a garment from the count
     * silently, which is the one outcome O3 cannot tolerate.
     */
    @Query(
        """SELECT * FROM scan
            WHERE syncState IN ('QUEUED', 'FAILED')
            ORDER BY capturedAt ASC
            LIMIT :limit"""
    )
    suspend fun pending(limit: Int): List<ScanEntity>

    @Query("SELECT COUNT(*) FROM scan WHERE syncState IN ('QUEUED', 'FAILED')")
    suspend fun pendingCount(): Int

    /** Acknowledged scans whose displayedAt never reached the host. */
    @Query(
        """SELECT * FROM scan
            WHERE syncState = 'ACKNOWLEDGED'
              AND displayedAt IS NOT NULL
              AND displayedAtSent = 0
            ORDER BY capturedAt ASC LIMIT :limit"""
    )
    suspend fun displayedNotSent(limit: Int): List<ScanEntity>

    @Query("UPDATE scan SET syncState = :state WHERE id = :id")
    suspend fun setState(id: Long, state: SyncState)

    @Query(
        """UPDATE scan SET syncState = 'ACKNOWLEDGED', serverScanId = :serverId,
                  recognitionResultId = :recogId, verdict = :verdict,
                  outcome = :outcome, canonicalSize = :size, engineMs = :engineMs,
                  message = :message, respondedAt = :respondedAt,
                  syncedAt = :syncedAt, lastError = NULL
            WHERE id = :id"""
    )
    suspend fun acknowledge(
        id: Long, serverId: Long, recogId: Long?, verdict: String,
        outcome: String?, size: String?, engineMs: Int?, message: String?,
        respondedAt: Long, syncedAt: Long,
    )

    @Query(
        """UPDATE scan SET syncState = 'FAILED', attempts = attempts + 1,
                  lastError = :error WHERE id = :id"""
    )
    suspend fun fail(id: Long, error: String?)

    @Query("UPDATE scan SET displayedAt = :millis WHERE id = :id")
    suspend fun setDisplayedAt(id: Long, millis: Long)

    @Query("UPDATE scan SET displayedAtSent = 1 WHERE id = :id")
    suspend fun markDisplayedSent(id: Long)

    @Query("UPDATE scan SET imagePath = NULL WHERE id = :id")
    suspend fun clearImagePath(id: Long)

    @Query("UPDATE scan SET lateAlertShown = 1 WHERE id = :id")
    suspend fun markLateAlertShown(id: Long)

    /**
     * Mismatches that came back after the scan was queued.
     *
     * These are the ones that matter operationally: the operator packed
     * without a verdict, so the carton may already be closed. §5.5 reports how
     * often this happened, which is why `queuedAt` and `syncedAt` are kept
     * rather than overwritten.
     */
    @Query(
        """SELECT * FROM scan
            WHERE verdict = 'mismatch' AND lateAlertShown = 0
              AND syncedAt IS NOT NULL AND syncedAt > queuedAt + 2000
            ORDER BY syncedAt DESC"""
    )
    suspend fun unshownLateMismatches(): List<ScanEntity>

    @Query("SELECT * FROM scan ORDER BY capturedAt DESC LIMIT :limit")
    suspend fun recent(limit: Int): List<ScanEntity>

    @Query(
        """SELECT COUNT(*) FROM scan
            WHERE checklistId = :checklistId AND canonicalSize = :size
              AND verdict IN ('match', 'mismatch')"""
    )
    suspend fun countForSize(checklistId: Int, size: String): Int

    @Query("SELECT COUNT(*) FROM scan WHERE cartonId = :cartonId")
    suspend fun countInCarton(cartonId: Int): Int

    /**
     * Per-size count inside one carton.
     *
     * Scoped to the carton, not the checklist. The breakdown on the carton
     * screen is what is in the box in front of the operator; a checklist-wide
     * figure there would show the shipment's running total and look like a
     * carton that is wildly over-filled.
     */
    @Query(
        """SELECT COUNT(*) FROM scan
            WHERE cartonId = :cartonId AND canonicalSize = :size"""
    )
    suspend fun countSizeInCarton(cartonId: Int, size: String): Int

    @Query("SELECT COUNT(*) FROM scan WHERE checklistId = :checklistId")
    suspend fun countForChecklist(checklistId: Int): Int

    @Query("DELETE FROM scan WHERE syncState = 'ACKNOWLEDGED' "
        + "AND displayedAtSent = 1 AND capturedAt < :before")
    suspend fun pruneSettled(before: Long): Int
}

@Dao
interface ManualEntryDao {

    @Insert
    suspend fun insert(entry: ManualEntryEntity): Long

    @Query("SELECT * FROM manual_entry WHERE id = :id")
    suspend fun byId(id: Long): ManualEntryEntity?

    @Query("SELECT * FROM manual_entry WHERE sent = 0 ORDER BY capturedAt ASC "
        + "LIMIT :limit")
    suspend fun pending(limit: Int): List<ManualEntryEntity>

    @Query("SELECT COUNT(*) FROM manual_entry WHERE sent = 0")
    suspend fun pendingCount(): Int

    @Query("UPDATE manual_entry SET sent = 1, lastError = NULL WHERE id = :id")
    suspend fun markSent(id: Long)

    @Query("UPDATE manual_entry SET attempts = attempts + 1, lastError = :error "
        + "WHERE id = :id")
    suspend fun fail(id: Long, error: String?)

    /** How many of the pending entries are bypasses. Shown on the queue screen. */
    @Query("SELECT COUNT(*) FROM manual_entry WHERE recognitionResultId IS NULL")
    suspend fun bypassCount(): Int
}

@Dao
interface CacheDao {

    @Upsert
    suspend fun putChecklist(row: ChecklistCacheEntity)

    @Query("SELECT * FROM checklist_cache WHERE checklistId = :id")
    suspend fun checklist(id: Int): ChecklistCacheEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putBarcodes(rows: List<BarcodeCacheEntity>)

    @Query("DELETE FROM barcode_cache WHERE checklistId = :id")
    suspend fun clearBarcodes(id: Int)

    /**
     * Resolve a scanned hangtag offline.
     *
     * Scoped to the checklist on purpose. A barcode from another style is not
     * a size error — it is the wrong garment at this station, and the server's
     * context check reports it as such. Resolving it against the whole cache
     * would hide that.
     */
    @Query("SELECT * FROM barcode_cache WHERE checklistId = :checklistId "
        + "AND barcode = :barcode LIMIT 1")
    suspend fun barcode(checklistId: Int, barcode: String): BarcodeCacheEntity?

    @Query("SELECT COUNT(*) FROM barcode_cache WHERE checklistId = :id")
    suspend fun barcodeCount(id: Int): Int

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putSizeMap(rows: List<SizeMapCacheEntity>)

    @Query("SELECT * FROM size_map_cache WHERE checklistId = :id")
    suspend fun sizeMap(id: Int): List<SizeMapCacheEntity>

    @Query("SELECT canonical FROM size_map_cache WHERE checklistId = :id "
        + "AND notation = :notation LIMIT 1")
    suspend fun canonicalFor(id: Int, notation: String): String?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putExpectations(rows: List<ExpectationCacheEntity>)

    @Query("SELECT * FROM expectation_cache WHERE checklistId = :id "
        + "ORDER BY sortOrder ASC")
    suspend fun expectations(id: Int): List<ExpectationCacheEntity>
}
