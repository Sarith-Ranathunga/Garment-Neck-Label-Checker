package lk.ac.nibm.gnlc

import android.app.Application
import lk.ac.nibm.gnlc.work.SyncScheduler

class GnlcApp : Application() {
    override fun onCreate() {
        super.onCreate()
        // Registered here rather than in an Activity so a phone that is
        // opened, used and backgrounded still has the periodic sweep running.
        SyncScheduler.ensurePeriodic(this)
    }
}
