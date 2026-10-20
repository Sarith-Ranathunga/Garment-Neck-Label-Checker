package lk.ac.nibm.gnlc.util

import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * Timestamps.
 *
 * Times are held as epoch milliseconds inside the app and converted to
 * ISO-8601 with an offset only when they are sent. Storing formatted strings
 * in Room would make arithmetic on them — which is what §5.7 does — depend on
 * parsing, and a scan that sat in the queue overnight would carry a string
 * whose offset no longer matches the device's.
 *
 * `java.time` is available from API 26, which is this app's minimum, so there
 * is no need for the desugaring library.
 */
object Time {

    private val ISO: DateTimeFormatter = DateTimeFormatter.ISO_OFFSET_DATE_TIME

    fun nowMillis(): Long = System.currentTimeMillis()

    /** An instant the FastAPI service will parse into a `datetime`. */
    fun iso(millis: Long): String =
        OffsetDateTime.ofInstant(Instant.ofEpochMilli(millis), ZoneId.systemDefault())
            .format(ISO)

    fun isoNow(): String = iso(nowMillis())

    /** `14:07:33`, for the queue list. */
    fun clock(millis: Long): String =
        OffsetDateTime.ofInstant(Instant.ofEpochMilli(millis), ZoneId.systemDefault())
            .format(DateTimeFormatter.ofPattern("HH:mm:ss"))

    /** `1.45 s`, or `870 ms` below a second. Used on the result panel. */
    fun humanMs(ms: Long): String =
        if (ms < 1000) "$ms ms" else String.format("%.2f s", ms / 1000.0)
}
