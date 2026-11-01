package lk.ac.nibm.gnlc.util

import android.content.Context
import android.content.SharedPreferences
import java.util.UUID

/**
 * Device settings.
 *
 * The server address lives here rather than in the source. The reason is
 * recovery time: when the host PC is replaced, keeping the address a setting
 * means the supervisor types a new one into each phone, which takes minutes.
 * A hard-coded address means rebuilding and reinstalling the app, which takes
 * hours and needs someone who can open Android Studio. §4.9 records this.
 */
class Prefs(context: Context) {

    private val sp: SharedPreferences =
        context.getSharedPreferences("gnlc", Context.MODE_PRIVATE)

    /** e.g. `http://192.168.1.50:8000`. Empty until the supervisor sets it. */
    var baseUrl: String
        get() = sp.getString(KEY_BASE_URL, "").orEmpty()
        set(value) = sp.edit().putString(KEY_BASE_URL, normaliseUrl(value)).apply()

    /**
     * Identifies this handset. Generated once and never regenerated: the
     * server deduplicates on `(device_id, local_id)`, so a device whose id
     * changed would have its retried scans counted twice and the
     * reconciliation totals in O3 would drift.
     */
    val deviceId: String
        get() {
            sp.getString(KEY_DEVICE_ID, null)?.let { return it }
            val generated = "ANDROID-" + UUID.randomUUID().toString()
                .take(8).uppercase()
            sp.edit().putString(KEY_DEVICE_ID, generated).apply()
            return generated
        }

    var operatorId: Int
        get() = sp.getInt(KEY_OPERATOR_ID, 0)
        set(value) = sp.edit().putInt(KEY_OPERATOR_ID, value).apply()

    var operatorName: String
        get() = sp.getString(KEY_OPERATOR_NAME, "").orEmpty()
        set(value) = sp.edit().putString(KEY_OPERATOR_NAME, value).apply()

    /**
     * The last username that signed in successfully, pre-filled on the sign-in
     * screen. Kept across sign-out on purpose: the same operator uses the same
     * handset shift after shift, and the password is what the control needs
     * retyped, not the name. No password is ever stored.
     */
    var lastUsername: String
        get() = sp.getString(KEY_LAST_USERNAME, "").orEmpty()
        set(value) = sp.edit().putString(KEY_LAST_USERNAME, value).apply()

    val isConfigured: Boolean get() = baseUrl.isNotBlank()
    val isSignedIn: Boolean get() = operatorId > 0

    fun signOut() {
        sp.edit().remove(KEY_OPERATOR_ID).remove(KEY_OPERATOR_NAME).apply()
    }

    companion object {
        private const val KEY_BASE_URL = "base_url"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_OPERATOR_ID = "operator_id"
        private const val KEY_OPERATOR_NAME = "operator_name"
        private const val KEY_LAST_USERNAME = "last_username"

        /**
         * Accepts what a supervisor will actually type.
         *
         * `192.168.1.50:8000` with no scheme, or an address with a trailing
         * slash, are both the commonest things to enter and neither is an
         * error worth refusing. Retrofit requires a scheme and a trailing
         * slash, so they are added here instead of being complained about.
         */
        fun normaliseUrl(raw: String): String {
            var s = raw.trim()
            if (s.isEmpty()) return ""
            if (!s.startsWith("http://") && !s.startsWith("https://")) {
                s = "http://$s"
            }
            if (!s.endsWith("/")) s = "$s/"
            return s
        }
    }
}
