package lk.ac.nibm.gnlc.ui

import android.content.Intent
import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import lk.ac.nibm.gnlc.R
import lk.ac.nibm.gnlc.databinding.ActivitySignInBinding
import lk.ac.nibm.gnlc.net.ApiProvider
import lk.ac.nibm.gnlc.net.LoginRequest
import lk.ac.nibm.gnlc.net.UserDto
import lk.ac.nibm.gnlc.util.Prefs
import kotlinx.coroutines.launch

/**
 * Who is scanning.
 *
 * There is no password, and that is a declared limitation rather than an
 * omission. This is a shared floor handset operated with gloves on, and a
 * password prompt at every shift change is exactly the friction DP3 predicts
 * will be worked around — in this case by one operator's account being used
 * all day, which would make the per-operator figures in §5.6 meaningless. The
 * device is inside the factory network and the account can only scan. §4.9
 * states it; §6.4 lists authentication as further work.
 *
 * The operator still has to be identified, because O4's before-and-after
 * comparison is per-operator and an anonymous scan cannot be attributed.
 */
class SignInActivity : AppCompatActivity() {

    private lateinit var b: ActivitySignInBinding
    private lateinit var prefs: Prefs

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivitySignInBinding.inflate(layoutInflater)
        setContentView(b.root)
        prefs = Prefs(this)

        b.settingsButton.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        b.retryButton.setOnClickListener { load() }
    }

    override fun onResume() {
        super.onResume()
        b.serverLine.text = if (prefs.isConfigured) prefs.baseUrl else "—"
        load()
    }

    private fun load() {
        b.operatorContainer.removeAllViews()
        b.retryButton.visibility = View.GONE

        if (!prefs.isConfigured) {
            showNote(getString(R.string.server_not_set))
            return
        }
        val api = ApiProvider.get(this) ?: run {
            showNote(getString(R.string.server_not_set)); return
        }

        showNote(null)
        lifecycleScope.launch {
            val people = runCatching { api.operators() }.getOrNull()
            if (people == null) {
                showNote(
                    "Cannot reach the host at ${prefs.baseUrl}\n\n" +
                        "Check that the supervisor PC is on and that this " +
                        "phone is on the factory WiFi."
                )
                b.retryButton.visibility = View.VISIBLE
                return@launch
            }
            if (people.isEmpty()) {
                showNote("The host has no active operators set up.")
                return@launch
            }
            people.forEach { addOperatorButton(it) }
        }
    }

    private fun addOperatorButton(user: UserDto) {
        val button = MaterialButton(
            this, null,
            com.google.android.material.R.attr.materialButtonOutlinedStyle,
        ).apply {
            text = user.displayName
            textSize = 17f
            minHeight = dp(64)
            isAllCaps = false
            setOnClickListener { signIn(user) }
        }
        val lp = android.widget.LinearLayout.LayoutParams(
            android.widget.LinearLayout.LayoutParams.MATCH_PARENT,
            android.widget.LinearLayout.LayoutParams.WRAP_CONTENT,
        ).apply { bottomMargin = dp(8) }
        b.operatorContainer.addView(button, lp)
    }

    private fun signIn(user: UserDto) {
        val api = ApiProvider.get(this) ?: return
        lifecycleScope.launch {
            // The call confirms the account is still active on the host before
            // the phone starts attributing scans to it.
            val confirmed = runCatching {
                api.login(LoginRequest(user.username, prefs.deviceId))
            }.getOrNull() ?: user

            prefs.operatorId = confirmed.id
            prefs.operatorName = confirmed.displayName
            startActivity(Intent(this@SignInActivity, ChecklistActivity::class.java))
        }
    }

    private fun showNote(text: String?) {
        b.emptyNote.text = text.orEmpty()
        b.emptyNote.visibility = if (text == null) View.GONE else View.VISIBLE
    }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()
}
