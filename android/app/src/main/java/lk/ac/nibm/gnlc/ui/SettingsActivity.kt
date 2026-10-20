package lk.ac.nibm.gnlc.ui

import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import lk.ac.nibm.gnlc.R
import lk.ac.nibm.gnlc.databinding.ActivitySettingsBinding
import lk.ac.nibm.gnlc.net.ApiProvider
import lk.ac.nibm.gnlc.util.Prefs
import kotlinx.coroutines.launch

/**
 * The server address, and a way to find out whether it works.
 *
 * The test button exists because the alternative is a supervisor typing an
 * address, going to the packing line, and discovering it is wrong from an
 * operator who cannot scan. Verifying it here costs one tap and turns a
 * half-hour of confusion into a corrected digit.
 */
class SettingsActivity : AppCompatActivity() {

    private lateinit var b: ActivitySettingsBinding
    private lateinit var prefs: Prefs

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(b.root)
        prefs = Prefs(this)

        b.serverInput.setText(prefs.baseUrl)
        b.deviceIdValue.text = prefs.deviceId

        b.testButton.setOnClickListener { test() }
        b.saveButton.setOnClickListener { save() }
    }

    private fun typedUrl(): String =
        Prefs.normaliseUrl(b.serverInput.text?.toString().orEmpty())

    private fun test() {
        val url = typedUrl()
        if (url.isBlank()) {
            result("Enter an address first.", ok = false); return
        }

        // Saved before testing, because ApiProvider builds its client from the
        // stored value. Saving an address that then fails its test is harmless
        // — nothing uses it until the operator scans — and it means the
        // supervisor does not have to type it twice.
        prefs.baseUrl = url
        ApiProvider.invalidate()
        b.serverInput.setText(prefs.baseUrl)

        val api = ApiProvider.get(this) ?: run {
            result("That address could not be used.", ok = false); return
        }

        result("Checking…", ok = null)
        lifecycleScope.launch {
            val health = runCatching { api.health() }
            if (health.isSuccess) {
                result(
                    "Connected. The host answered and its database is up.",
                    ok = true,
                )
            } else {
                result(
                    "No answer from $url\n\n" +
                        "Check: the supervisor PC is on, this phone is on the " +
                        "same WiFi, the address and port are right, and the " +
                        "PC's firewall allows port 8000.",
                    ok = false,
                )
            }
        }
    }

    private fun save() {
        prefs.baseUrl = typedUrl()
        ApiProvider.invalidate()
        finish()
    }

    private fun result(text: String, ok: Boolean?) {
        b.testResult.visibility = View.VISIBLE
        b.testResult.text = text
        b.testResult.setTextColor(
            when (ok) {
                true -> ContextCompat.getColor(this, R.color.verdict_match)
                false -> ContextCompat.getColor(this, R.color.verdict_mismatch)
                null -> ContextCompat.getColor(this, R.color.ink_muted)
            }
        )
    }
}
