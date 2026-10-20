package lk.ac.nibm.gnlc.ui

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import lk.ac.nibm.gnlc.R
import lk.ac.nibm.gnlc.data.ScanRepository
import lk.ac.nibm.gnlc.databinding.ActivityCartonBinding
import lk.ac.nibm.gnlc.databinding.DialogCloseCartonBinding
import lk.ac.nibm.gnlc.net.ApiProvider
import lk.ac.nibm.gnlc.net.CartonCloseRequest
import lk.ac.nibm.gnlc.net.CartonOpenRequest
import lk.ac.nibm.gnlc.util.Prefs
import kotlinx.coroutines.launch

/**
 * Stage 6 — carton packing.
 *
 * The running count comes from the scans, not from a number the operator
 * keeps. When the carton closes, its declared contents and its verified
 * contents are the same figure because they were derived from the same events
 * — and that identity is what makes O3's four-point reconciliation mean
 * anything. A typed count would reconcile with itself and prove nothing.
 */
class CartonActivity : AppCompatActivity() {

    private lateinit var b: ActivityCartonBinding
    private lateinit var repo: ScanRepository
    private lateinit var prefs: Prefs

    private var checklistId = 0
    private var shipmentId = 0
    private var cartonId: Int? = null
    private var cartonNo: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityCartonBinding.inflate(layoutInflater)
        setContentView(b.root)
        repo = ScanRepository(this)
        prefs = Prefs(this)

        checklistId = intent.getIntExtra(ChecklistActivity.EXTRA_CHECKLIST_ID, 0)
        shipmentId = intent.getIntExtra(ChecklistActivity.EXTRA_SHIPMENT_ID, 0)

        b.openButton.setOnClickListener { openCarton() }
        b.scanButton.setOnClickListener { scanGarment() }
        b.closeButton.setOnClickListener { confirmClose() }

        lifecycleScope.launch { header() }
        suggestNextNumber()
    }

    override fun onResume() {
        super.onResume()
        refreshCount()
    }

    private suspend fun header() {
        val cached = repo.cachedChecklist(checklistId)
        b.headerLine.text = getString(R.string.carton)
        b.subLine.text = buildString {
            append(cached?.buyerCode ?: "")
            cached?.styleNo?.let { append(" · $it") }
            cached?.colour?.let { append(" · $it") }
            cached?.shipmentReference?.let { append("  ·  $it") }
        }.trim()
    }

    /**
     * Proposes the next carton number from what the host already has.
     *
     * Carton numbers are unique per shipment, so a guess that collides is an
     * error the operator has to interpret. Asking the host first turns that
     * into a pre-filled field. It is only a suggestion: it stays editable,
     * because the floor's numbering may not be a simple sequence.
     */
    private fun suggestNextNumber() {
        val api = ApiProvider.get(this) ?: return
        lifecycleScope.launch {
            val existing = runCatching { api.cartons(shipmentId) }.getOrNull()
                ?: return@launch
            val next = existing
                .mapNotNull { it.cartonNo.trim().toIntOrNull() }
                .maxOrNull()
                ?.plus(1)
                ?: 1
            if (b.cartonNoInput.text.isNullOrBlank()) {
                b.cartonNoInput.setText(next.toString())
            }
            // An already-open carton is resumed rather than left stranded. A
            // phone that ran out of battery mid-carton must not force the
            // operator to start a new one and split the count in two.
            existing.firstOrNull { it.closedAt == null }?.let { open ->
                note(
                    "Carton ${open.cartonNo} is still open with " +
                        "${open.packed} garments in it. Enter that number to " +
                        "carry on with it."
                )
            }
        }
    }

    private fun openCarton() {
        val typed = b.cartonNoInput.text?.toString()?.trim().orEmpty()
        if (typed.isBlank()) {
            note("Enter a carton number."); return
        }
        val api = ApiProvider.get(this) ?: run {
            // Opening a carton needs the host, because the carton id it
            // returns is what every scan in the carton is filed against.
            // Inventing one locally would produce scans pointing at a carton
            // that does not exist.
            note(
                "A carton cannot be opened without the host — the carton " +
                    "number has to come from it. Scans already on this phone " +
                    "are safe."
            )
            return
        }

        b.openButton.isEnabled = false
        lifecycleScope.launch {
            val carton = runCatching {
                api.openCarton(
                    CartonOpenRequest(shipmentId, typed, prefs.operatorId)
                )
            }.getOrNull()

            b.openButton.isEnabled = true
            if (carton == null) {
                note("Could not open that carton. Check the connection and retry.")
                return@launch
            }
            cartonId = carton.id
            cartonNo = carton.cartonNo
            note(null)
            b.openPanel.visibility = View.GONE
            b.activePanel.visibility = View.VISIBLE
            b.headerLine.text = getString(R.string.carton) + " ${carton.cartonNo}"
            refreshCount()
        }
    }

    /** Each garment goes through the same S5 check, filed against this carton. */
    private fun scanGarment() {
        val id = cartonId ?: return
        startActivity(
            Intent(this, CaptureActivity::class.java).apply {
                putExtra(ChecklistActivity.EXTRA_CHECKLIST_ID, checklistId)
                putExtra(ChecklistActivity.EXTRA_SHIPMENT_ID, shipmentId)
                putExtra(ChecklistActivity.EXTRA_STAGE, "S6")
                putExtra(ChecklistActivity.EXTRA_CARTON_ID, id)
                putExtra(ChecklistActivity.EXTRA_CARTON_NO, cartonNo)
            }
        )
    }

    /**
     * The count, from the phone's own rows.
     *
     * Local rather than fetched, deliberately. The operator needs the number
     * to be right while the WiFi is down, and the phone knows what it has
     * recorded whether or not any of it has been sent yet. The host's count
     * catches up when the queue drains.
     */
    private fun refreshCount() {
        val id = cartonId ?: return
        lifecycleScope.launch {
            val packed = repo.countInCarton(id)
            b.countValue.text = packed.toString()
            b.countCaption.text = "garments in carton ${cartonNo.orEmpty()}"

            b.breakdownContainer.removeAllViews()
            val pending = repo.pendingCount()
            if (pending > 0) {
                addLine(
                    "$pending of these are still waiting to send — their " +
                        "verdicts are not back yet.",
                    R.color.verdict_no_read,
                )
            }
            // Scoped to this carton, not the checklist. The breakdown is what
            // is in the box in front of the operator; a shipment-wide figure
            // here would read as a carton holding thousands of garments.
            repo.cachedExpectations(checklistId).forEach { exp ->
                val counted = repo.countSizeInCarton(id, exp.size)
                addLine("${exp.size}   $counted", R.color.ink_muted)
            }
        }
    }

    private fun addLine(text: String, colorRes: Int) {
        val view = TextView(this).apply {
            this.text = text
            textSize = 15f
            setTextColor(ContextCompat.getColor(this@CartonActivity, colorRes))
            setPadding(0, dp(6), 0, dp(6))
        }
        b.breakdownContainer.addView(
            view,
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ),
        )
    }

    /**
     * Closing freezes the contents, so it asks first — and it refuses while
     * scans are still queued.
     *
     * A carton closed with unsent scans would have its contents frozen from an
     * incomplete set of events, and the missing garments would reappear in the
     * queue afterwards with nowhere to go. The reconciliation would then show
     * a shortfall that looks like a packing error and is not.
     */
    private fun confirmClose() {
        val id = cartonId ?: return
        lifecycleScope.launch {
            val pending = repo.pendingCount()
            if (pending > 0) {
                AlertDialog.Builder(this@CartonActivity)
                    .setTitle(R.string.close_carton)
                    .setMessage(
                        "$pending scans have not reached the host yet.\n\n" +
                            "Closing now would freeze this carton's contents " +
                            "without them. Wait for the queue to clear, or " +
                            "move to where the WiFi reaches, then close it."
                    )
                    .setPositiveButton(R.string.ok, null)
                    .show()
                return@launch
            }

            val dialogBinding = DialogCloseCartonBinding.inflate(layoutInflater)
            val packed = repo.countInCarton(id)
            dialogBinding.contentsLine.text =
                "Carton ${cartonNo.orEmpty()} holds $packed garments, counted " +
                    "from the scans."

            AlertDialog.Builder(this@CartonActivity)
                .setTitle(R.string.confirm_close)
                .setView(dialogBinding.root)
                .setPositiveButton(R.string.close_carton) { _, _ ->
                    close(
                        id,
                        dialogBinding.netInput.text?.toString()?.toDoubleOrNull(),
                        dialogBinding.grossInput.text?.toString()?.toDoubleOrNull(),
                    )
                }
                .setNegativeButton(R.string.cancel, null)
                .show()
        }
    }

    private fun close(id: Int, net: Double?, gross: Double?) {
        val api = ApiProvider.get(this) ?: return
        lifecycleScope.launch {
            val result = runCatching {
                api.closeCarton(id, CartonCloseRequest(net, gross))
            }.getOrNull()

            if (result == null) {
                note("Could not close the carton. Check the connection and retry.")
                return@launch
            }

            val contents = result.contents
                .joinToString("\n") { "${it.size}   ${it.qty}" }
            AlertDialog.Builder(this@CartonActivity)
                .setTitle("Carton ${cartonNo.orEmpty()} closed")
                .setMessage(
                    "${result.total} garments\n\n$contents\n\n" +
                        "Contents were taken from the scans, not typed in."
                )
                .setPositiveButton(R.string.ok) { _, _ ->
                    cartonId = null
                    cartonNo = null
                    b.activePanel.visibility = View.GONE
                    b.openPanel.visibility = View.VISIBLE
                    b.cartonNoInput.setText("")
                    b.headerLine.text = getString(R.string.carton)
                    suggestNextNumber()
                }
                .setCancelable(false)
                .show()
        }
    }

    private fun note(text: String?) {
        b.statusNote.text = text.orEmpty()
        b.statusNote.visibility = if (text == null) View.GONE else View.VISIBLE
    }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()
}
