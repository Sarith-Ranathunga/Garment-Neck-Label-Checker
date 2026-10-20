package lk.ac.nibm.gnlc.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.graphics.Rect
import android.graphics.RectF
import android.os.Build
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.util.Log
import android.view.View
import android.view.WindowManager
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.UseCase
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.mlkit.vision.barcode.BarcodeScanner
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import lk.ac.nibm.gnlc.R
import lk.ac.nibm.gnlc.data.ScanRepository
import lk.ac.nibm.gnlc.databinding.ActivityCaptureBinding
import lk.ac.nibm.gnlc.util.Time
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Capture and verdict, for stages 1 and 5.
 *
 * S1 has one step: photograph the neck label. There is no hangtag yet, so
 * nothing exists to compare against — the size is *established* here, not
 * verified, and the wording on screen keeps those apart because the research
 * does.
 *
 * S5 has two: scan the hangtag barcode, then photograph the neck label. The
 * barcode is decoded on the device by ML Kit and resolved against the cached
 * map, so the hangtag side of the comparison keeps working when the WiFi does
 * not. Only the neck label needs the server, because only the neck label needs
 * OCR.
 */
class CaptureActivity : AppCompatActivity() {

    private lateinit var b: ActivityCaptureBinding
    private lateinit var repo: ScanRepository

    private var checklistId = 0
    private var shipmentId = 0
    private var stage = "S1"
    private var cartonId: Int? = null
    private var cartonNo: String? = null

    private var imageCapture: ImageCapture? = null
    private var analysis: ImageAnalysis? = null
    private lateinit var cameraExecutor: ExecutorService
    private var scanner: BarcodeScanner? = null

    /** S5 only. Null until the hangtag has been read. */
    private var hangtagBarcode: String? = null
    private var hangtagSize: String? = null
    private var hangtagStyle: String? = null
    private var hangtagColour: String? = null

    /** Guards the analyser, which fires many frames a second. */
    private val barcodeBusy = AtomicBoolean(false)
    private var capturing = false

    /** The row of the scan currently shown, for the displayed_at call. */
    private var shownRowId: Long? = null
    private var shownServerScanId: Long? = null
    private var shownRecognitionId: Long? = null
    private var shownWasNoRead = false

    private val requestCamera = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted -> if (granted) startCamera() else showPermissionPanel() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityCaptureBinding.inflate(layoutInflater)
        setContentView(b.root)

        repo = ScanRepository(this)
        cameraExecutor = Executors.newSingleThreadExecutor()

        checklistId = intent.getIntExtra(ChecklistActivity.EXTRA_CHECKLIST_ID, 0)
        shipmentId = intent.getIntExtra(ChecklistActivity.EXTRA_SHIPMENT_ID, 0)
        stage = intent.getStringExtra(ChecklistActivity.EXTRA_STAGE) ?: "S1"
        cartonId = intent.getIntExtra(ChecklistActivity.EXTRA_CARTON_ID, 0)
            .takeIf { it > 0 }
        cartonNo = intent.getStringExtra(ChecklistActivity.EXTRA_CARTON_NO)

        // The operator's hands are full and the screen going dark between
        // garments costs a tap every single time.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        b.captureButton.setOnClickListener { takePhoto() }
        b.nextButton.setOnClickListener { nextGarment() }
        b.manualButton.setOnClickListener { openManualEntry(bypass = true) }
        b.fallbackButton.setOnClickListener { openManualEntry(bypass = false) }
        b.grantButton.setOnClickListener { requestCamera.launch(Manifest.permission.CAMERA) }
        b.helpButton.setOnClickListener { askForHelp() }

        if (stage == "S5") {
            scanner = BarcodeScanning.getClient(
                BarcodeScannerOptions.Builder()
                    .setBarcodeFormats(
                        Barcode.FORMAT_UPC_A, Barcode.FORMAT_UPC_E,
                        Barcode.FORMAT_EAN_13, Barcode.FORMAT_EAN_8,
                        Barcode.FORMAT_CODE_128, Barcode.FORMAT_CODE_39,
                        Barcode.FORMAT_CODE_93, Barcode.FORMAT_ITF,
                    )
                    .build()
            )
        }

        updateHeader()
        ensureCamera()
    }

    override fun onResume() {
        super.onResume()
        refreshQueueLine()
        checkLateMismatches()
    }

    override fun onDestroy() {
        super.onDestroy()
        cameraExecutor.shutdown()
        scanner?.close()
    }

    // =============================================================== camera ==

    private fun ensureCamera() {
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.CAMERA,
        ) == PackageManager.PERMISSION_GRANTED
        if (granted) startCamera() else requestCamera.launch(Manifest.permission.CAMERA)
    }

    private fun showPermissionPanel() {
        b.permissionPanel.visibility = View.VISIBLE
    }

    private fun startCamera() {
        b.permissionPanel.visibility = View.GONE
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            val provider = runCatching { future.get() }.getOrNull() ?: return@addListener

            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(b.preview.surfaceProvider)
            }

            // MINIMIZE_LATENCY, not MAXIMIZE_QUALITY. Episode A's images read
            // at 93.75% with ordinary phone-camera quality, so the extra
            // processing time buys nothing measurable here and costs the
            // operator a wait on every garment.
            imageCapture = ImageCapture.Builder()
                .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                .build()

            val useCases = mutableListOf<UseCase>(preview, imageCapture!!)

            if (stage == "S5") {
                analysis = ImageAnalysis.Builder()
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .build()
                    .also { it.setAnalyzer(cameraExecutor, ::analyseFrame) }
                useCases += analysis!!
            }

            runCatching {
                provider.unbindAll()
                provider.bindToLifecycle(
                    this, CameraSelector.DEFAULT_BACK_CAMERA, *useCases.toTypedArray(),
                )
            }.onFailure { Log.e(TAG, "camera bind failed", it) }
        }, ContextCompat.getMainExecutor(this))
    }

    // ====================================================== hangtag barcode ==

    /**
     * Reads the hangtag from the live preview.
     *
     * Runs only until a barcode is found, then stops looking. A scanner that
     * kept firing would re-read the same hangtag while the operator was
     * lining up the neck label and silently replace the expected size with
     * whatever drifted through the frame.
     */
    @androidx.annotation.OptIn(ExperimentalGetImage::class)
    private fun analyseFrame(proxy: ImageProxy) {
        if (hangtagBarcode != null || !barcodeBusy.compareAndSet(false, true)) {
            proxy.close(); return
        }
        val media = proxy.image
        if (media == null) {
            barcodeBusy.set(false); proxy.close(); return
        }

        val input = InputImage.fromMediaImage(media, proxy.imageInfo.rotationDegrees)
        scanner?.process(input)
            ?.addOnSuccessListener { codes ->
                codes.firstOrNull { !it.rawValue.isNullOrBlank() }
                    ?.rawValue
                    ?.let { onHangtagRead(it) }
            }
            ?.addOnCompleteListener {
                barcodeBusy.set(false)
                proxy.close()
            }
            ?: run { barcodeBusy.set(false); proxy.close() }
    }

    private fun onHangtagRead(raw: String) {
        if (hangtagBarcode != null) return
        hangtagBarcode = raw

        lifecycleScope.launch {
            val hit = repo.resolveHangtag(checklistId, raw)
            if (hit == null) {
                // Not a mismatch. An unknown barcode is a data gap, and
                // presenting it as a size error would be a false positive
                // against O2 — and would have the operator pull a garment
                // that is very likely fine.
                hangtagSize = null
                hangtagStyle = null
                hangtagColour = null
                b.stepLine.text =
                    "Hangtag read, but it is not in the system. " +
                        "Photograph the label; the supervisor will be told."
            } else {
                hangtagSize = hit.buyerNotation ?: hit.size
                hangtagStyle = hit.styleNo
                hangtagColour = hit.colour
                b.stepLine.text = "Hangtag: ${hit.buyerNotation ?: hit.size}" +
                    "  ·  now photograph the neck label"
            }
            b.hintLine.text = getString(R.string.photograph_help)
            b.captureButton.isEnabled = true
            b.captureButton.text = getString(R.string.capture)
        }
    }

    // ============================================================== capture ==

    private fun takePhoto() {
        val capture = imageCapture ?: return
        if (capturing) return

        if (stage == "S5" && hangtagBarcode == null) {
            b.hintLine.text = getString(R.string.scan_hangtag_help)
            return
        }

        capturing = true
        busy(true)

        // The first of the three timestamps §5.7 needs. Taken here, before the
        // shutter, because the figure being reported is what the operator
        // experiences from the moment they act.
        val capturedAt = Time.nowMillis()
        val target = File(cacheDir, "scan_${capturedAt}.jpg")

        capture.takePicture(
            ImageCapture.OutputFileOptions.Builder(target).build(),
            cameraExecutor,
            object : ImageCapture.OnImageSavedCallback {
                override fun onError(exc: ImageCaptureException) {
                    Log.e(TAG, "capture failed", exc)
                    runOnUiThread {
                        capturing = false
                        busy(false)
                        b.hintLine.text = "The camera failed. Try again."
                    }
                }

                override fun onImageSaved(output: ImageCapture.OutputFileResults) {
                    // This callback runs on the camera executor. `lifecycleScope`
                    // registers a lifecycle observer on first use, and that has
                    // to happen on the main thread — launching straight from
                    // here throws on the very first photograph the app ever
                    // takes, which is the worst possible time to find out.
                    runOnUiThread {
                        // The crop rectangle is read off the views now, while
                        // still on the main thread. Reading View.getWidth()
                        // from the IO dispatcher is unsafe, and a zero would
                        // silently disable cropping and send the whole frame.
                        val region = frameRegion()
                        lifecycleScope.launch { submit(target, capturedAt, region) }
                    }
                }
            },
        )
    }

    private suspend fun submit(file: File, capturedAt: Long, region: RectF?) {
        val prepared = withContext(Dispatchers.IO) { cropAndCompress(file, region) }

        val result = repo.capture(
            stage = stage,
            checklistId = checklistId,
            cartonId = cartonId,
            imageFile = prepared,
            hangtagBarcode = hangtagBarcode,
            hangtagSizeRaw = hangtagSize,
            scannedStyle = hangtagStyle,
            scannedColour = hangtagColour,
            capturedAt = capturedAt,
        )

        capturing = false
        busy(false)
        showVerdict(result, capturedAt)
    }

    /**
     * Crop to the frame, then compress.
     *
     * Both steps matter, for different reasons. The crop removes the packing
     * table, the operator's hands and the next garment, which is what lets the
     * constrained-vocabulary parse work on text rather than on a scene. The
     * compression is what keeps the upload under the 900 KB the server accepts
     * and off the critical path of the end-to-end figure, on an access point
     * shared with the rest of the building.
     *
     * Quality 80: Episode A's images were well within this and still read at
     * 93.75%.
     */
    private fun cropAndCompress(file: File, region: RectF?): File {
        return try {
            val full = BitmapFactory.decodeFile(file.absolutePath) ?: return file
            val rotated = applyExifRotation(file, full)
            val cropped = cropToFrame(rotated, region)

            var quality = 80
            var bytes = compress(cropped, quality)
            // Step down rather than fail. A rejected upload would send the
            // operator back to re-photograph a garment they have already put
            // down.
            while (bytes.size > MAX_BYTES && quality > 40) {
                quality -= 10
                bytes = compress(cropped, quality)
            }

            val out = File(cacheDir, "s_${file.name}")
            out.outputStream().use { it.write(bytes) }
            if (out != file) file.delete()
            if (cropped != rotated) cropped.recycle()
            if (rotated != full) rotated.recycle()
            full.recycle()
            out
        } catch (e: Exception) {
            Log.w(TAG, "crop/compress failed, sending the original", e)
            file
        }
    }

    private fun compress(bitmap: Bitmap, quality: Int): ByteArray =
        ByteArrayOutputStream().also {
            bitmap.compress(Bitmap.CompressFormat.JPEG, quality, it)
        }.toByteArray()

    /**
     * Where the aiming frame sits, as fractions of the preview.
     *
     * **Main thread only.** It reads view geometry, which is why it is called
     * before the work moves to the IO dispatcher rather than inside it. The
     * fractions, not the pixels, are what travel: the photograph is a
     * different resolution from the preview, so an absolute rectangle would
     * crop the wrong part of it.
     *
     * Returns null when the views have not been measured yet, in which case
     * the full frame is sent uncropped — a larger upload, but never a crop of
     * the wrong region.
     */
    private fun frameRegion(): RectF? {
        val preview = b.preview
        val box = b.frameBox
        if (preview.width == 0 || preview.height == 0 ||
            box.width == 0 || box.height == 0
        ) return null

        // A margin around the frame, because a label pressed right against
        // the edge of the box would otherwise lose its first character.
        val pad = 0.04f
        val left = box.left.toFloat() / preview.width
        val top = box.top.toFloat() / preview.height
        return RectF(
            (left - pad).coerceAtLeast(0f),
            (top - pad).coerceAtLeast(0f),
            (left + box.width.toFloat() / preview.width + pad).coerceAtMost(1f),
            (top + box.height.toFloat() / preview.height + pad).coerceAtMost(1f),
        )
    }

    /**
     * Crop the photograph to the region the operator was aiming at.
     *
     * The box on screen and the region that actually gets read are the same
     * rectangle, on any screen size, because both come from the same measured
     * views. A hard-coded fraction would drift on a different handset and the
     * operator would be carefully filling a box that is not what gets sent.
     */
    private fun cropToFrame(source: Bitmap, region: RectF?): Bitmap {
        if (region == null) return source

        val rect = Rect(
            (region.left * source.width).toInt(),
            (region.top * source.height).toInt(),
            (region.right * source.width).toInt(),
            (region.bottom * source.height).toInt(),
        )
        if (rect.width() < 64 || rect.height() < 64) return source
        if (rect.right > source.width || rect.bottom > source.height) return source

        return Bitmap.createBitmap(
            source, rect.left, rect.top, rect.width(), rect.height(),
        )
    }

    /**
     * Apply the EXIF orientation, so the server sees the label the way the
     * operator did.
     *
     * The recogniser tries four rotations anyway, so this is not what makes a
     * sideways label readable — it is what stops the first attempt wasting the
     * rotation that was always going to work.
     */
    private fun applyExifRotation(file: File, bitmap: Bitmap): Bitmap {
        val exif = runCatching {
            androidx.exifinterface.media.ExifInterface(file.absolutePath)
        }.getOrNull() ?: return bitmap

        val degrees = when (
            exif.getAttributeInt(
                androidx.exifinterface.media.ExifInterface.TAG_ORIENTATION,
                androidx.exifinterface.media.ExifInterface.ORIENTATION_NORMAL,
            )
        ) {
            androidx.exifinterface.media.ExifInterface.ORIENTATION_ROTATE_90 -> 90f
            androidx.exifinterface.media.ExifInterface.ORIENTATION_ROTATE_180 -> 180f
            androidx.exifinterface.media.ExifInterface.ORIENTATION_ROTATE_270 -> 270f
            else -> 0f
        }
        if (degrees == 0f) return bitmap

        return Bitmap.createBitmap(
            bitmap, 0, 0, bitmap.width, bitmap.height,
            Matrix().apply { postRotate(degrees) }, true,
        )
    }

    // ============================================================== verdict ==

    private fun showVerdict(result: ScanRepository.Outcome, capturedAt: Long) {
        shownRowId = result.localRowId
        shownServerScanId = result.serverScanId
        shownRecognitionId = result.recognitionResultId
        shownWasNoRead = result.verdict == "no_read"

        val (word, fg, bg) = when {
            result.queued -> Triple(
                R.string.verdict_queued, R.color.verdict_queued,
                R.color.verdict_queued_bg,
            )
            result.verdict == "match" -> Triple(
                R.string.verdict_match, R.color.verdict_match,
                R.color.verdict_match_bg,
            )
            result.verdict == "mismatch" -> Triple(
                R.string.verdict_mismatch, R.color.verdict_mismatch,
                R.color.verdict_mismatch_bg,
            )
            result.verdict == "out_of_context" -> Triple(
                R.string.verdict_out_of_context, R.color.verdict_mismatch,
                R.color.verdict_mismatch_bg,
            )
            else -> Triple(
                R.string.verdict_no_read, R.color.verdict_no_read,
                R.color.verdict_no_read_bg,
            )
        }

        b.verdictPanel.setBackgroundColor(ContextCompat.getColor(this, bg))
        b.verdictWord.setText(word)
        b.verdictWord.setTextColor(ContextCompat.getColor(this, fg))

        b.verdictSize.text = result.size.orEmpty()
        b.verdictSize.visibility =
            if (result.size.isNullOrBlank()) View.GONE else View.VISIBLE

        b.verdictDetail.text = when {
            // A queued scan is never dressed up as a success. The operator
            // packed without a verdict and has to know it: the check still
            // runs when the queue syncs, but possibly after the carton is
            // closed. §5.5 reports how often that happened.
            result.queued -> getString(R.string.queued_explain)
            result.verdict == "mismatch" ->
                result.message + "\n\n" + getString(R.string.set_aside)
            else -> result.message
        }

        b.verdictMeta.text = buildList<String> {
            result.endToEndMs?.let { add(Time.humanMs(it)) }
            result.engineMs?.takeIf { it > 0 }?.let { add("engine $it ms") }
            if (stage == "S5") hangtagSize?.let { add("hangtag $it") }
        }.joinToString("  ·  ")

        // The fallback is offered only where a recognition actually failed.
        // Offering it after a clean match would invite an operator to override
        // a correct reading, and the override would be indistinguishable from
        // a genuine fallback in the data.
        b.fallbackButton.visibility =
            if (shownWasNoRead && shownServerScanId != null &&
                shownRecognitionId != null
            ) View.VISIBLE else View.GONE

        b.verdictPanel.visibility = View.VISIBLE

        // The third timestamp: when the verdict actually reached the operator.
        shownRowId?.let { row ->
            lifecycleScope.launch { repo.markDisplayed(row) }
        }

        if (result.verdict == "mismatch") alert()
        refreshQueueLine()
    }

    private fun nextGarment() {
        b.verdictPanel.visibility = View.GONE
        hangtagBarcode = null
        hangtagSize = null
        hangtagStyle = null
        hangtagColour = null
        shownRowId = null
        shownServerScanId = null
        shownRecognitionId = null
        shownWasNoRead = false
        updateHeader()
        refreshQueueLine()
    }

    // ========================================================= manual entry ==

    /**
     * Opens manual entry on one of two different paths.
     *
     * [bypass] false is the fallback after a no-read: the id of the
     * recognition that failed goes with it, and the entry is a legitimate use
     * of the O1 escape hatch.
     *
     * [bypass] true is the operator skipping the scan altogether. Nothing is
     * sent that could make it look otherwise — not even a scan id — because
     * the proportion of these is the DP3 finding in §5.8, and a client that
     * tidied it away would make that finding unfalsifiable.
     */
    private fun openManualEntry(bypass: Boolean) {
        val intent = Intent(this, ManualEntryActivity::class.java).apply {
            putExtra(ChecklistActivity.EXTRA_CHECKLIST_ID, checklistId)
            putExtra(ChecklistActivity.EXTRA_STAGE, stage)
            cartonId?.let { putExtra(ChecklistActivity.EXTRA_CARTON_ID, it) }
            putExtra(ManualEntryActivity.EXTRA_IS_BYPASS, bypass)
            if (!bypass) {
                putExtra(ManualEntryActivity.EXTRA_SERVER_SCAN_ID, shownServerScanId ?: -1L)
                putExtra(
                    ManualEntryActivity.EXTRA_RECOGNITION_ID,
                    shownRecognitionId ?: -1L,
                )
            }
        }
        startActivity(intent)
        nextGarment()
    }

    // ================================================================ chrome ==

    private fun updateHeader() {
        lifecycleScope.launch {
            val cached = repo.cachedChecklist(checklistId)
            b.contextLine.text = buildString {
                append(cached?.buyerCode ?: "")
                cached?.styleNo?.let { append(" · $it") }
                cached?.colour?.let { append(" · $it") }
                cartonNo?.let { append("  ·  carton $it") }
            }.trim().ifBlank { "Checklist $checklistId" }

            if (stage == "S5" && hangtagBarcode == null) {
                b.stepLine.text = getString(R.string.scan_hangtag)
                b.hintLine.text = getString(R.string.scan_hangtag_help)
                // Disabled until the hangtag is read. At S5 a photograph with
                // no hangtag has nothing to be compared against, so allowing
                // it would create a scan the research cannot use.
                b.captureButton.isEnabled = false
                b.captureButton.text = getString(R.string.scan_hangtag)
            } else {
                b.stepLine.text = getString(R.string.photograph_label)
                b.hintLine.text = getString(R.string.photograph_help)
                b.captureButton.isEnabled = true
                b.captureButton.text = getString(R.string.capture)
            }
        }
    }

    private fun refreshQueueLine() {
        lifecycleScope.launch {
            val pending = repo.pendingCount()
            b.queueLine.text = getString(R.string.pending_count, pending)
            b.queueLine.visibility = if (pending > 0) View.VISIBLE else View.GONE
        }
    }

    /**
     * A verdict that arrived after the scan was queued.
     *
     * This is the case that needs an interruption rather than a line of text:
     * the garment was packed without a verdict and the carton may already be
     * closed, so someone has to go and find it.
     */
    private fun checkLateMismatches() {
        lifecycleScope.launch {
            repo.lateMismatches().forEach { row ->
                AlertDialog.Builder(this@CaptureActivity)
                    .setTitle(R.string.verdict_mismatch)
                    .setMessage(
                        "A scan that was queued has come back as a MISMATCH.\n\n" +
                            "Taken at ${Time.clock(row.capturedAt)}" +
                            (row.canonicalSize?.let { ", label read as $it" } ?: "") +
                            (row.hangtagSizeRaw?.let { ", hangtag $it" } ?: "") +
                            "\n\nFind this garment. If its carton is closed it " +
                            "may need reopening."
                    )
                    .setPositiveButton(R.string.ok, null)
                    .setCancelable(false)
                    .show()
                repo.markLateAlertShown(row.id)
            }
        }
    }

    private fun askForHelp() {
        val causes = arrayOf(
            "Label is unreadable",
            "Barcode will not scan",
            "Wrong garment at this station",
            "App is not responding",
            "Something else",
        )
        AlertDialog.Builder(this)
            .setTitle(R.string.need_help)
            .setItems(causes) { _, which ->
                lifecycleScope.launch {
                    repo.assistance(stage, shipmentId.takeIf { it > 0 }, causes[which])
                }
                b.hintLine.text = "Recorded. Tell the supervisor as well."
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    /**
     * A mismatch buzzes as well as turning the screen red.
     *
     * The operator may be looking at the garment rather than the phone at the
     * moment the verdict lands, and a mismatch is the one outcome that must
     * not be missed.
     */
    private fun alert() {
        runCatching {
            val vibrator = ContextCompat.getSystemService(this, Vibrator::class.java)
            val pattern = longArrayOf(0, 180, 90, 180)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                vibrator?.vibrate(VibrationEffect.createWaveform(pattern, -1))
            } else {
                @Suppress("DEPRECATION")
                vibrator?.vibrate(pattern, -1)
            }
        }
    }

    private fun busy(show: Boolean) {
        b.busy.visibility = if (show) View.VISIBLE else View.GONE
        b.captureButton.isEnabled = !show
    }

    companion object {
        private const val TAG = "GnlcCapture"
        /** The server's limit. Staying under it is the device's job. */
        private const val MAX_BYTES = 880_000
    }
}
