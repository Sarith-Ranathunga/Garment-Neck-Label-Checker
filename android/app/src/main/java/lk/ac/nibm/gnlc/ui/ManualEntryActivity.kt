package lk.ac.nibm.gnlc.ui

import android.graphics.Color
import android.os.Bundle
import android.widget.GridLayout
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import lk.ac.nibm.gnlc.R
import lk.ac.nibm.gnlc.data.ScanRepository
import lk.ac.nibm.gnlc.databinding.ActivityManualEntryBinding
import kotlinx.coroutines.launch

/**
 * The operator types the size.
 *
 * Two different things arrive at this screen and they must not be conflated:
 *
 *  - **Fallback.** Recognition ran and returned no-read. O1 requires that no
 *    garment can be blocked indefinitely, so the operator proceeds, and the
 *    entry is recorded against the recognition that actually failed.
 *
 *  - **Bypass.** The operator skipped the scan. This is the behaviour DP3
 *    predicts when the compliant path is slower than the shortcut, and the
 *    proportion of these is the §5.8 finding.
 *
 * The distinction is preserved three times over: a different flag in the
 * intent, a different field in the local row, and a different endpoint on the
 * host, where the server — not this client — writes the null foreign key. It
 * is laboured on purpose. A single careless line here would make every bypass
 * look like a legitimate fallback, §5.8 would report a figure nobody could
 * challenge, and the artefact's ability to report on its own weakness would be
 * gone without anyone noticing.
 */
class ManualEntryActivity : AppCompatActivity() {

    private lateinit var b: ActivityManualEntryBinding
    private lateinit var repo: ScanRepository

    private var checklistId = 0
    private var cartonId: Int? = null
    private var stage = "S1"
    private var isBypass = true
    private var serverScanId: Long = -1
    private var recognitionId: Long = -1

    private var chosen: String? = null
    private val keys = mutableListOf<MaterialButton>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityManualEntryBinding.inflate(layoutInflater)
        setContentView(b.root)
        repo = ScanRepository(this)

        checklistId = intent.getIntExtra(ChecklistActivity.EXTRA_CHECKLIST_ID, 0)
        cartonId = intent.getIntExtra(ChecklistActivity.EXTRA_CARTON_ID, 0)
            .takeIf { it > 0 }
        stage = intent.getStringExtra(ChecklistActivity.EXTRA_STAGE) ?: "S1"
        isBypass = intent.getBooleanExtra(EXTRA_IS_BYPASS, true)
        serverScanId = intent.getLongExtra(EXTRA_SERVER_SCAN_ID, -1)
        recognitionId = intent.getLongExtra(EXTRA_RECOGNITION_ID, -1)

        // A "fallback" with nothing to point at is not a fallback. Rather than
        // sending a plausible-looking id, it is treated as what it actually
        // is: a manual entry with no recognition behind it.
        if (!isBypass && (serverScanId <= 0 || recognitionId <= 0)) isBypass = true

        showPath()
        loadSizes()

        b.confirmButton.setOnClickListener { confirm() }
        b.cancelButton.setOnClickListener { finish() }
    }

    private fun showPath() {
        if (isBypass) {
            b.pathNote.text = getString(R.string.bypass_warning)
            b.pathNote.setTextColor(
                ContextCompat.getColor(this, R.color.verdict_no_read))
            b.pathNote.setBackgroundColor(
                ContextCompat.getColor(this, R.color.verdict_no_read_bg))
        } else {
            b.pathNote.text = getString(R.string.fallback_note)
            b.pathNote.setTextColor(
                ContextCompat.getColor(this, R.color.ink_muted))
            b.pathNote.setBackgroundColor(Color.TRANSPARENT)
        }
    }

    /**
     * The keypad is the shipment's allocated sizes and nothing wider.
     *
     * A free text field would let a size that is not in this shipment into the
     * carton contents and from there into the reconciliation, where it would
     * look like real data. The closed set makes that entry unreachable rather
     * than caught later — the same constraint the recogniser parses against.
     */
    private fun loadSizes() {
        lifecycleScope.launch {
            val cached = repo.cachedChecklist(checklistId)
            val sizes = cached?.validSizes
                ?.split(",")
                ?.map { it.trim() }
                ?.filter { it.isNotEmpty() }
                .orEmpty()

            if (sizes.isEmpty()) {
                b.pathNote.text =
                    "This checklist's size list is not on the phone, so a " +
                        "size cannot be entered safely. Reconnect and open " +
                        "the checklist again."
                b.confirmButton.isEnabled = false
                return@launch
            }
            sizes.forEach { addKey(it) }
        }
    }

    private fun addKey(size: String) {
        val key = MaterialButton(
            this, null,
            com.google.android.material.R.attr.materialButtonOutlinedStyle,
        ).apply {
            text = size
            textSize = 22f
            minHeight = dp(72)
            minWidth = 0
            insetTop = 0
            insetBottom = 0
            setOnClickListener { choose(size) }
        }
        val lp = GridLayout.LayoutParams().apply {
            width = 0
            height = GridLayout.LayoutParams.WRAP_CONTENT
            columnSpec = GridLayout.spec(GridLayout.UNDEFINED, 1, 1f)
            setMargins(dp(4), dp(4), dp(4), dp(4))
        }
        keys += key
        b.sizeContainer.addView(key, lp)
    }

    private fun choose(size: String) {
        chosen = size
        keys.forEach { key ->
            val selected = key.text == size
            key.setTextColor(
                ContextCompat.getColor(
                    this,
                    if (selected) R.color.white else R.color.ink,
                )
            )
            key.setBackgroundColor(
                ContextCompat.getColor(
                    this,
                    if (selected) R.color.brand else android.R.color.transparent,
                )
            )
        }
        b.confirmButton.isEnabled = true
    }

    private fun confirm() {
        val size = chosen ?: return
        val reason = b.reasonInput.text?.toString()?.trim()?.ifBlank { null }

        b.confirmButton.isEnabled = false
        lifecycleScope.launch {
            if (isBypass) {
                repo.bypass(
                    enteredSize = size,
                    reason = reason,
                    stage = stage,
                    checklistId = checklistId.takeIf { it > 0 },
                    cartonId = cartonId,
                )
            } else {
                repo.manualFallback(
                    serverScanId = serverScanId,
                    recognitionResultId = recognitionId,
                    enteredSize = size,
                    reason = reason,
                    stage = stage,
                    checklistId = checklistId.takeIf { it > 0 },
                    cartonId = cartonId,
                )
            }
            // The entry is on disk either way, so finishing here is safe
            // whether or not it reached the host. A failed send is in the
            // queue, not lost.
            finish()
        }
    }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()

    companion object {
        const val EXTRA_IS_BYPASS = "is_bypass"
        const val EXTRA_SERVER_SCAN_ID = "server_scan_id"
        const val EXTRA_RECOGNITION_ID = "recognition_id"
    }
}
