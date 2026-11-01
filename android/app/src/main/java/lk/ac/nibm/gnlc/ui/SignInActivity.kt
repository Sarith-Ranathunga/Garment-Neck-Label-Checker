package lk.ac.nibm.gnlc.ui

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.view.inputmethod.EditorInfo
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import lk.ac.nibm.gnlc.R
import lk.ac.nibm.gnlc.databinding.ActivitySignInBinding
import lk.ac.nibm.gnlc.net.ApiProvider
import lk.ac.nibm.gnlc.net.LoginRequest
import lk.ac.nibm.gnlc.util.Prefs
import kotlinx.coroutines.launch
import retrofit2.HttpException

/**
 * Who is scanning.
 *
 * This screen used to list every active account as a button and sign the
 * operator in on one tap, with no password. That was a declared limitation
 * (§4.9, §6.4) and it has now been closed, because the picker had a second
 * problem beyond authentication: two people on the floor share a display
 * name, so the list showed two identical buttons and the per-operator figures
 * in §5.6 were only as reliable as the operator's guess about which one was
 * theirs. A username is unique by construction; a display name is not.
 *
 * The cost is the friction DP3 warns about — a password prompt at a shift
 * change is exactly the kind of step that gets worked around, in this case by
 * one account staying signed in all day. Three things keep that cost low:
 * the sign-in survives the app being closed, so it is typed once per shift
 * and not once per garment; the keyboard opens on the username field with the
 * password field one tap away; and the supervisor sets short passwords (four
 * characters is the floor) precisely because this is a gloved handset on a
 * factory floor rather than a bank.
 *
 * The password is sent to the host and never stored on the phone. Only the
 * operator's id and display name are kept, which is what the scan payload
 * needs. Note that the handsets reach the host over plain HTTP, so the
 * password crosses the factory LAN in the clear — the hash protects the
 * stored password, not the wire. TLS on the host remains further work.
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
        b.signInButton.setOnClickListener { signIn() }

        // Enter on the password field submits, so the operator never has to
        // dismiss the keyboard to find the button behind it.
        b.passwordInput.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_DONE) { signIn(); true } else false
        }
    }

    override fun onResume() {
        super.onResume()
        b.serverLine.text = if (prefs.isConfigured) prefs.baseUrl else "—"
        b.passwordInput.setText("")

        // The username is left as typed. The same operator signs in on the
        // same handset shift after shift, and retyping it each time is the
        // friction, not the password.
        b.usernameInput.setText(prefs.lastUsername)

        if (!prefs.isConfigured) {
            setFormEnabled(false)
            note(getString(R.string.server_not_set))
        } else {
            setFormEnabled(true)
            note(null)
        }
    }

    private fun signIn() {
        val username = b.usernameInput.text.toString().trim()
        val password = b.passwordInput.text.toString()

        if (username.isEmpty() || password.isEmpty()) {
            note(getString(R.string.sign_in_incomplete)); return
        }
        val api = ApiProvider.get(this) ?: run {
            note(getString(R.string.server_not_set)); return
        }

        setFormEnabled(false)
        note(getString(R.string.signing_in))

        lifecycleScope.launch {
            val result = runCatching {
                api.login(LoginRequest(username, password, prefs.deviceId))
            }
            val user = result.getOrNull()

            if (user == null) {
                val cause = result.exceptionOrNull()
                // 401 is a wrong username or password; the host deliberately
                // does not say which, so neither does this screen. Anything
                // else is the network or the host being down, which is a
                // different problem with a different fix.
                note(
                    if (cause is HttpException && cause.code() == 401) {
                        getString(R.string.sign_in_rejected)
                    } else {
                        getString(R.string.host_unreachable, prefs.baseUrl)
                    }
                )
                setFormEnabled(true)
                b.passwordInput.setText("")
                return@launch
            }

            prefs.operatorId = user.id
            prefs.operatorName = user.displayName
            prefs.lastUsername = user.username
            b.passwordInput.setText("")
            startActivity(Intent(this@SignInActivity, ChecklistActivity::class.java))
        }
    }

    private fun setFormEnabled(enabled: Boolean) {
        b.usernameInput.isEnabled = enabled
        b.passwordInput.isEnabled = enabled
        b.signInButton.isEnabled = enabled
    }

    private fun note(text: String?) {
        b.noteLine.text = text.orEmpty()
        b.noteLine.visibility = if (text == null) View.GONE else View.VISIBLE
    }
}
