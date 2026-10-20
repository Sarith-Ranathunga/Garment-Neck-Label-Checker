package lk.ac.nibm.gnlc.ui

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import lk.ac.nibm.gnlc.R
import lk.ac.nibm.gnlc.data.ScanEntity
import lk.ac.nibm.gnlc.data.ScanRepository
import lk.ac.nibm.gnlc.data.SyncState
import lk.ac.nibm.gnlc.databinding.ActivityQueueBinding
import lk.ac.nibm.gnlc.databinding.ItemQueueBinding
import lk.ac.nibm.gnlc.util.Time
import lk.ac.nibm.gnlc.work.SyncScheduler
import kotlinx.coroutines.launch

/**
 * What this phone is holding.
 *
 * The queue is shown to the operator rather than hidden behind a developer
 * flag. A scan that has not been sent means a garment was packed before any
 * verdict existed for it, and the person who packed it is the one who needs to
 * know — concealing it would make the artefact look better and the packing
 * worse. It is also what lets a supervisor see at a glance whether a phone is
 * genuinely offline or simply idle.
 */
class QueueActivity : AppCompatActivity() {

    private lateinit var b: ActivityQueueBinding
    private lateinit var repo: ScanRepository
    private val adapter = QueueAdapter()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityQueueBinding.inflate(layoutInflater)
        setContentView(b.root)
        repo = ScanRepository(this)

        b.queueList.layoutManager = LinearLayoutManager(this)
        b.queueList.adapter = adapter

        b.syncButton.setOnClickListener {
            SyncScheduler.requestSyncNow(this)
            b.summaryLine.text = "Sending…"
            // Re-read shortly afterwards. The worker runs out of process, so
            // there is nothing to await here.
            b.queueList.postDelayed({ load() }, 2_500L)
        }
    }

    override fun onResume() {
        super.onResume()
        load()
    }

    private fun load() {
        lifecycleScope.launch {
            val rows = repo.recent(200)
            val pending = repo.pendingCount()

            b.summaryLine.text = if (pending == 0) {
                "Everything on this phone has reached the host."
            } else {
                getString(R.string.pending_count, pending) +
                    " — these garments have no verdict yet."
            }

            adapter.submit(rows)
            b.emptyNote.visibility = if (rows.isEmpty()) View.VISIBLE else View.GONE
        }
    }
}

class QueueAdapter : RecyclerView.Adapter<QueueAdapter.Row>() {

    private val items = mutableListOf<ScanEntity>()

    fun submit(rows: List<ScanEntity>) {
        items.clear()
        items += rows
        notifyDataSetChanged()
    }

    override fun getItemCount() = items.size

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Row =
        Row(
            ItemQueueBinding.inflate(
                LayoutInflater.from(parent.context), parent, false,
            )
        )

    override fun onBindViewHolder(holder: Row, position: Int) =
        holder.bind(items[position])

    class Row(private val v: ItemQueueBinding) : RecyclerView.ViewHolder(v.root) {

        fun bind(row: ScanEntity) {
            val context = v.root.context

            // The same three colours the verdict panel uses, so the states
            // mean the same thing on both screens.
            val colour = when {
                row.syncState != SyncState.ACKNOWLEDGED -> R.color.verdict_no_read
                row.verdict == "match" -> R.color.verdict_match
                row.verdict == "mismatch" -> R.color.verdict_mismatch
                else -> R.color.verdict_queued
            }
            v.stateBar.setBackgroundColor(ContextCompat.getColor(context, colour))

            v.line1.text = when {
                row.syncState != SyncState.ACKNOWLEDGED -> "Waiting to send"
                row.verdict == "match" -> "Match — ${row.canonicalSize.orEmpty()}"
                row.verdict == "mismatch" -> "MISMATCH"
                row.verdict == "out_of_context" -> "Wrong garment"
                row.verdict == "no_read" -> "No read"
                else -> row.verdict?.replaceFirstChar { it.uppercase() } ?: "—"
            }

            v.line2.text = buildList<String> {
                add(stageLabel(row.stage))
                row.hangtagSizeRaw?.let { add("hangtag $it") }
                if (row.syncState != SyncState.ACKNOWLEDGED) {
                    row.lastError?.let { add(it) }
                    if (row.attempts > 0) add("${row.attempts} tries")
                }
                // A verdict that arrived long after the scan was queued is
                // flagged here too, because that is the case where the carton
                // may already have been closed. §5.5 counts these.
                val queueDelay = row.syncedAt?.minus(row.queuedAt) ?: 0
                if (row.syncState == SyncState.ACKNOWLEDGED && queueDelay > 2000) {
                    add("verdict ${Time.humanMs(queueDelay)} late")
                }
            }.joinToString("  ·  ")

            v.timeLabel.text = Time.clock(row.capturedAt)
        }

        private fun stageLabel(stage: String) = when (stage) {
            "S1" -> "First segregation"
            "S5" -> "Hangtag check"
            "S6" -> "Carton"
            else -> stage
        }
    }
}
