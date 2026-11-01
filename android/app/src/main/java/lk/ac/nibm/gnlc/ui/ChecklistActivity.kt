package lk.ac.nibm.gnlc.ui

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.LinearLayout
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import lk.ac.nibm.gnlc.R
import lk.ac.nibm.gnlc.data.ScanRepository
import lk.ac.nibm.gnlc.databinding.ActivityChecklistBinding
import lk.ac.nibm.gnlc.net.ApiProvider
import lk.ac.nibm.gnlc.net.ChecklistDto
import lk.ac.nibm.gnlc.util.Prefs
import kotlinx.coroutines.launch

/**
 * Which checklist the operator is working on.
 *
 * The stage comes from the checklist rather than being chosen by the operator.
 * S1, S5 and S6 do different things — S1 establishes a size, S5 verifies one
 * against a hangtag, S6 counts into a carton — and letting the operator pick
 * the stage would let them run the wrong one against the right garments, which
 * produces data that looks valid and is not.
 */
class ChecklistActivity : AppCompatActivity() {

    private lateinit var b: ActivityChecklistBinding
    private lateinit var prefs: Prefs
    private lateinit var repo: ScanRepository

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityChecklistBinding.inflate(layoutInflater)
        setContentView(b.root)
        prefs = Prefs(this)
        repo = ScanRepository(this)

        b.refreshButton.setOnClickListener { load() }
        b.queueButton.setOnClickListener {
            startActivity(Intent(this, QueueActivity::class.java))
        }
    }

    override fun onResume() {
        super.onResume()
        b.operatorLine.text = prefs.operatorName.ifBlank { "—" }
        load()
        refreshQueueBadge()
    }

    private fun refreshQueueBadge() {
        lifecycleScope.launch {
            val pending = repo.pendingCount()
            b.queueButton.text = if (pending > 0) {
                getString(R.string.queue_button) + " ($pending)"
            } else {
                getString(R.string.queue_button)
            }
        }
    }

    private fun load() {
        b.checklistContainer.removeAllViews()
        val api = ApiProvider.get(this)
        if (api == null) {
            note(getString(R.string.server_not_set)); return
        }
        note(null)

        lifecycleScope.launch {
            // The operator id is sent so the host returns only shipments this
            // operator is assigned to. Without it the list would show work
            // that /api/scan refuses, which is a dead end discovered with a
            // garment already in hand.
            val lists = runCatching {
                api.checklists(operatorId = prefs.operatorId)
            }.getOrNull()
            if (lists == null) {
                note(
                    "Cannot reach the host.\n\n" +
                        "Scans already taken are safe on this phone and will " +
                        "send themselves when the connection returns."
                )
                return@launch
            }
            if (lists.isEmpty()) {
                note(getString(R.string.no_checklists)); return@launch
            }
            lists.forEach { addChecklistButton(it) }
        }
    }

    private fun addChecklistButton(c: ChecklistDto) {
        val button = MaterialButton(
            this, null,
            com.google.android.material.R.attr.materialButtonOutlinedStyle,
        ).apply {
            text = buildString {
                append(stageLabel(c.stage))
                append('\n')
                append("${c.buyerCode} · ${c.styleNo} · ${c.colour}")
                append('\n')
                append("${c.shipmentReference} · ${c.scanned} scanned")
            }
            textSize = 15f
            minHeight = dp(84)
            isAllCaps = false
            gravity = android.view.Gravity.START or android.view.Gravity.CENTER_VERTICAL
            setPadding(dp(16), dp(12), dp(16), dp(12))
            setOnClickListener { open(c) }
        }
        b.checklistContainer.addView(
            button,
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { bottomMargin = dp(10) },
        )
    }

    /** Plain words, because the stage numbers mean nothing on the floor. */
    private fun stageLabel(stage: String): String = when (stage) {
        "S1" -> "First size segregation"
        "S5" -> "Second segregation — check against hangtag"
        "S6" -> "Carton packing"
        else -> stage
    }

    private fun open(c: ChecklistDto) {
        lifecycleScope.launch {
            // The bundle is fetched before the camera opens. Doing it here
            // rather than lazily means an operator who walks out of WiFi range
            // after starting already has the barcode map and size list on the
            // device, and can keep working.
            val ready = repo.loadBundle(c.id)
            if (!ready) {
                note(
                    "Could not load this checklist's data, and nothing is " +
                        "cached for it yet. Move to where the WiFi reaches " +
                        "and try again."
                )
                return@launch
            }

            val intent = if (c.stage == "S6") {
                Intent(this@ChecklistActivity, CartonActivity::class.java)
            } else {
                Intent(this@ChecklistActivity, CaptureActivity::class.java)
            }
            intent.putExtra(EXTRA_CHECKLIST_ID, c.id)
            intent.putExtra(EXTRA_SHIPMENT_ID, c.shipmentId)
            intent.putExtra(EXTRA_STAGE, c.stage)
            startActivity(intent)
        }
    }

    private fun note(text: String?) {
        b.emptyNote.text = text.orEmpty()
        b.emptyNote.visibility = if (text == null) View.GONE else View.VISIBLE
        b.refreshButton.visibility = if (text == null) View.GONE else View.VISIBLE
    }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()

    companion object {
        const val EXTRA_CHECKLIST_ID = "checklist_id"
        const val EXTRA_SHIPMENT_ID = "shipment_id"
        const val EXTRA_STAGE = "stage"
        const val EXTRA_CARTON_ID = "carton_id"
        const val EXTRA_CARTON_NO = "carton_no"
    }
}
