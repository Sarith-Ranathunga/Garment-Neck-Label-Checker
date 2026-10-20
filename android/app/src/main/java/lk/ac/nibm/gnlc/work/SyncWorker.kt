package lk.ac.nibm.gnlc.work

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import lk.ac.nibm.gnlc.data.Db
import lk.ac.nibm.gnlc.data.ScanRepository
import lk.ac.nibm.gnlc.net.ApiProvider
import lk.ac.nibm.gnlc.net.DisplayedAtRequest
import lk.ac.nibm.gnlc.util.Time

/**
 * Drains the offline queue.
 *
 * WorkManager rather than a coroutine in an Activity, and the difference is
 * not stylistic: a coroutine dies with the process. A phone that is put down,
 * locked, and picked up twenty minutes later must still send the scans taken
 * before the WiFi dropped, and so must a phone that was rebooted in between.
 * Nothing else on Android survives both.
 */
class SyncWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val repo = ScanRepository(applicationContext)
        val db = Db.get(applicationContext)

        if (ApiProvider.get(applicationContext) == null) {
            // No address configured. Retrying would spin until the supervisor
            // sets one, so stop and let the next explicit request wake us.
            return Result.success()
        }

        var failed = 0
        var sent = 0

        // --- scans, oldest capture first --------------------------------
        //
        // The order matters. Carton contents and the four-point
        // reconciliation are derived from these events, so a queue drained
        // newest-first would interleave scans from either side of a carton
        // close and the counts would not add up.
        for (row in db.scans().pending(limit = BATCH)) {
            if (repo.trySend(row.id) != null) sent++ else failed++
        }

        // --- manual entries and bypasses --------------------------------
        for (row in db.manualEntries().pending(limit = BATCH)) {
            if (repo.sendManual(row.id)) sent++ else failed++
        }

        // --- the third timestamp, for scans displayed while offline -----
        //
        // Without this pass, every verdict shown during an outage would be
        // missing from the §5.7 latency figure, which would then describe only
        // the scans that happened while the network was healthy — the fast
        // ones. That is a biased sample, not a smaller one.
        val api = ApiProvider.get(applicationContext)
        if (api != null) {
            for (row in db.scans().displayedNotSent(limit = BATCH)) {
                val serverId = row.serverScanId ?: continue
                val shown = row.displayedAt ?: continue
                runCatching {
                    api.markDisplayed(serverId, DisplayedAtRequest(Time.iso(shown)))
                    db.scans().markDisplayedSent(row.id)
                }.onFailure { failed++ }
            }
        }

        // Settled rows older than a fortnight are pruned. Their evidence is
        // on the host by then, and an unbounded local table on a shared floor
        // phone eventually fills the device.
        runCatching {
            db.scans().pruneSettled(Time.nowMillis() - FOURTEEN_DAYS)
        }

        return when {
            failed == 0 -> Result.success()
            // Retry, not failure. WorkManager applies exponential backoff, so
            // a host that is down for an hour is polled a handful of times
            // rather than hammered — and the scans still go when it returns.
            else -> Result.retry()
        }
    }

    companion object {
        private const val BATCH = 50
        private const val FOURTEEN_DAYS = 14L * 24 * 60 * 60 * 1000
    }
}
