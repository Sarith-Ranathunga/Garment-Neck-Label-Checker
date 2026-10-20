package lk.ac.nibm.gnlc.work

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit

/**
 * When the queue gets drained.
 *
 * Two triggers, for two different situations:
 *
 *  - an immediate attempt after a scan fails to send, so a momentary drop
 *    costs seconds rather than minutes;
 *  - a periodic sweep, so a phone nobody is scanning on still catches up — a
 *    handset left on a bench at the end of a shift with queued scans must not
 *    hold them until someone next opens the app.
 *
 * Fifteen minutes is the shortest period WorkManager allows. It is also the
 * interval the host's USB backup runs on, so a queued scan is on the backup
 * within half an hour of being taken in the worst case.
 */
object SyncScheduler {

    private const val PERIODIC = "gnlc-sync-periodic"
    private const val IMMEDIATE = "gnlc-sync-now"

    private val constraints = Constraints.Builder()
        .setRequiredNetworkType(NetworkType.CONNECTED)
        .build()

    /** Called once at app start. Idempotent. */
    fun ensurePeriodic(context: Context) {
        val request = PeriodicWorkRequestBuilder<SyncWorker>(
            15, TimeUnit.MINUTES,
        )
            .setConstraints(constraints)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()

        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            PERIODIC,
            // KEEP, not UPDATE. Replacing the work on every app start would
            // reset the period each time and a phone opened frequently would
            // never actually reach a scheduled run.
            ExistingPeriodicWorkPolicy.KEEP,
            request,
        )
    }

    /**
     * Try now.
     *
     * APPEND_OR_REPLACE rather than REPLACE: a sync already in flight is
     * allowed to finish and this one follows it. Cancelling a run that is
     * halfway through a batch would leave the rest of that batch for the next
     * trigger, which on a bad connection means the queue never finishes
     * draining.
     */
    fun requestSyncNow(context: Context) {
        val request = OneTimeWorkRequestBuilder<SyncWorker>()
            .setConstraints(constraints)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
            .build()

        WorkManager.getInstance(context).enqueueUniqueWork(
            IMMEDIATE, ExistingWorkPolicy.APPEND_OR_REPLACE, request,
        )
    }
}
