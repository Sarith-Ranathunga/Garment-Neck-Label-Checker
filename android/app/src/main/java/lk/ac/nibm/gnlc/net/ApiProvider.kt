package lk.ac.nibm.gnlc.net

import android.content.Context
import lk.ac.nibm.gnlc.BuildConfig
import lk.ac.nibm.gnlc.util.Prefs
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

/**
 * Builds the Retrofit client, and rebuilds it when the server address changes.
 *
 * The timeouts are set for factory WiFi carrying a photograph, not for a
 * phone on a good connection. A read timeout of ten seconds was too short in
 * testing: the server's recognition pass alone takes most of a second, and the
 * upload before it is a few hundred kilobytes over an access point shared with
 * everything else in the building. A premature timeout is worse than a slow
 * reply, because the scan then retries and the operator waits twice.
 *
 * `retryOnConnectionFailure` is left on. A retry is safe here precisely
 * because every scan carries a `local_id` the server deduplicates on.
 */
object ApiProvider {

    @Volatile private var cached: Api? = null
    @Volatile private var cachedUrl: String = ""

    fun get(context: Context): Api? {
        val url = Prefs(context).baseUrl
        if (url.isBlank()) return null

        cached?.let { if (cachedUrl == url) return it }

        synchronized(this) {
            cached?.let { if (cachedUrl == url) return it }
            val api = build(url)
            cached = api
            cachedUrl = url
            return api
        }
    }

    /** Called after the address is edited, so the next call uses the new one. */
    fun invalidate() {
        synchronized(this) {
            cached = null
            cachedUrl = ""
        }
    }

    private fun build(baseUrl: String): Api {
        val logging = HttpLoggingInterceptor().apply {
            level = if (BuildConfig.DEBUG) HttpLoggingInterceptor.Level.BASIC
            else HttpLoggingInterceptor.Level.NONE
        }

        val client = OkHttpClient.Builder()
            .connectTimeout(8, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)   // the image upload
            .readTimeout(30, TimeUnit.SECONDS)    // upload + OCR + reply
            .retryOnConnectionFailure(true)
            .addInterceptor(logging)
            .build()

        return Retrofit.Builder()
            .baseUrl(baseUrl)
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(Api::class.java)
    }
}
