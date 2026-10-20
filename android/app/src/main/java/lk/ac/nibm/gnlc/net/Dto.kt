package lk.ac.nibm.gnlc.net

import com.google.gson.annotations.SerializedName

/*
 * These mirror the Pydantic models in server/app/main.py and
 * server/app/client_api.py. Field names are snake_case to match the wire
 * format, so there is nothing to configure on the Gson instance and no
 * silently-null field when a name drifts.
 *
 * Response fields that the server may legitimately omit are nullable. Gson
 * will write null into a non-null Kotlin field without complaining and the
 * crash then happens somewhere else entirely, so the nullability here is load
 * bearing rather than decorative.
 */

// ------------------------------------------------------------------ sign in --

data class LoginRequest(
    @SerializedName("username") val username: String,
    @SerializedName("device_id") val deviceId: String,
)

data class UserDto(
    @SerializedName("id") val id: Int,
    @SerializedName("username") val username: String,
    @SerializedName("display_name") val displayName: String,
    @SerializedName("role") val role: String,
)

// --------------------------------------------------------------- checklists --

data class ChecklistDto(
    @SerializedName("id") val id: Int,
    @SerializedName("shipment_id") val shipmentId: Int,
    @SerializedName("shipment_reference") val shipmentReference: String,
    @SerializedName("stage") val stage: String,
    @SerializedName("style_no") val styleNo: String,
    @SerializedName("colour") val colour: String,
    @SerializedName("buyer_code") val buyerCode: String,
    @SerializedName("scanned") val scanned: Int = 0,
)

data class SizeExpectationDto(
    @SerializedName("size") val size: String,
    @SerializedName("expected_qty") val expectedQty: Int,
    @SerializedName("verified_qty") val verifiedQty: Int,
)

data class BarcodeEntryDto(
    @SerializedName("barcode") val barcode: String,
    @SerializedName("symbology") val symbology: String?,
    @SerializedName("size") val size: String,
    @SerializedName("buyer_notation") val buyerNotation: String?,
    @SerializedName("style_no") val styleNo: String,
    @SerializedName("colour") val colour: String,
    @SerializedName("sku") val sku: String?,
)

/**
 * Everything the phone needs to keep working with the network down.
 *
 * `barcodes` is the field that matters most. A UPC-A hangtag encodes nothing
 * — not the size, not the style — so an offline phone without this map can
 * scan a hangtag and learn nothing from it, and the S5 comparison becomes
 * impossible. With the map cached, a WiFi outage only delays the neck-label
 * verdict; the hangtag still resolves on the device.
 */
data class ChecklistBundleDto(
    @SerializedName("checklist_id") val checklistId: Int,
    @SerializedName("shipment_id") val shipmentId: Int,
    @SerializedName("shipment_reference") val shipmentReference: String,
    @SerializedName("stage") val stage: String,
    @SerializedName("style_no") val styleNo: String,
    @SerializedName("colour") val colour: String,
    @SerializedName("buyer_id") val buyerId: Int,
    @SerializedName("buyer_code") val buyerCode: String,
    @SerializedName("valid_sizes") val validSizes: List<String> = emptyList(),
    @SerializedName("size_map") val sizeMap: Map<String, String> = emptyMap(),
    @SerializedName("items") val items: List<SizeExpectationDto> = emptyList(),
    @SerializedName("barcodes") val barcodes: List<BarcodeEntryDto> = emptyList(),
)

// --------------------------------------------------------------------- scan --

/**
 * The JSON half of the multipart scan request.
 *
 * Note what is absent: there is no field for a recognition result id and no
 * field for a verdict. The device reports what it observed and the server
 * decides what it means. That split is what keeps the O2 measurement
 * independent of the client.
 */
data class ScanPayload(
    @SerializedName("local_id") val localId: String,
    @SerializedName("device_id") val deviceId: String,
    @SerializedName("operator_id") val operatorId: Int,
    @SerializedName("checklist_id") val checklistId: Int?,
    @SerializedName("carton_id") val cartonId: Int?,
    @SerializedName("stage") val stage: String,
    @SerializedName("hangtag_barcode") val hangtagBarcode: String?,
    @SerializedName("hangtag_size_raw") val hangtagSizeRaw: String?,
    @SerializedName("scanned_style") val scannedStyle: String?,
    @SerializedName("scanned_colour") val scannedColour: String?,
    @SerializedName("captured_at") val capturedAt: String,
    @SerializedName("queued_at") val queuedAt: String?,
)

data class ScanResponseDto(
    @SerializedName("scan_event_id") val scanEventId: Long,
    /**
     * Sent back so the client never has to invent it. A manual entry after a
     * no-read posts this id; a manual entry with no scan behind it goes to
     * `/api/bypass`, where the server writes the NULL itself. Between the two
     * the client has no way to fabricate a recognition id, which is what
     * keeps the DP3 measurement in §5.8 honest.
     */
    @SerializedName("recognition_result_id") val recognitionResultId: Long?,
    @SerializedName("verdict") val verdict: String,
    @SerializedName("outcome") val outcome: String,
    @SerializedName("canonical_size") val canonicalSize: String?,
    @SerializedName("rotation_used") val rotationUsed: Int?,
    @SerializedName("confidence") val confidence: Double?,
    @SerializedName("engine_ms") val engineMs: Int = 0,
    @SerializedName("hangtag_size_raw") val hangtagSizeRaw: String?,
    @SerializedName("hangtag_source") val hangtagSource: String?,
    @SerializedName("message") val message: String = "",
)

// ------------------------------------------------------------ manual entry --

data class ManualEntryRequest(
    @SerializedName("scan_event_id") val scanEventId: Long,
    @SerializedName("entered_size") val enteredSize: String,
    /** Null is never sent from here — see [BypassRequest]. */
    @SerializedName("recognition_result_id") val recognitionResultId: Long,
    @SerializedName("reason") val reason: String?,
)

/**
 * A size entered with no recognition attempt behind it.
 *
 * This is a separate endpoint on purpose. If the app posted bypasses through
 * `/api/manual-entry` with a null id, one wrong line in this client would make
 * every bypass look like a legitimate fallback, and §5.8 would report a figure
 * nobody could challenge. Routing it here means the server writes the null,
 * and the client cannot misreport it even by accident.
 */
data class BypassRequest(
    @SerializedName("local_id") val localId: String,
    @SerializedName("device_id") val deviceId: String,
    @SerializedName("operator_id") val operatorId: Int,
    @SerializedName("checklist_id") val checklistId: Int?,
    @SerializedName("carton_id") val cartonId: Int?,
    @SerializedName("stage") val stage: String,
    @SerializedName("entered_size") val enteredSize: String,
    @SerializedName("captured_at") val capturedAt: String,
    @SerializedName("reason") val reason: String?,
)

data class BypassResponseDto(
    @SerializedName("scan_event_id") val scanEventId: Long,
    @SerializedName("exception_id") val exceptionId: Long,
    @SerializedName("bypass") val bypass: Boolean,
)

// ------------------------------------------------------------------ cartons --

data class CartonOpenRequest(
    @SerializedName("shipment_id") val shipmentId: Int,
    @SerializedName("carton_no") val cartonNo: String,
    @SerializedName("opened_by") val openedBy: Int,
)

data class CartonDto(
    @SerializedName("id") val id: Int,
    @SerializedName("shipment_id") val shipmentId: Int,
    @SerializedName("carton_no") val cartonNo: String,
    @SerializedName("closed_at") val closedAt: String?,
    @SerializedName("packed") val packed: Int = 0,
)

data class CartonCloseRequest(
    @SerializedName("net_weight_kg") val netWeightKg: Double?,
    @SerializedName("gross_weight_kg") val grossWeightKg: Double?,
)

data class CartonContentDto(
    @SerializedName("size") val size: String,
    @SerializedName("qty") val qty: Int,
)

data class CartonCloseResponseDto(
    @SerializedName("carton_id") val cartonId: Int,
    @SerializedName("closed") val closed: Boolean,
    @SerializedName("contents") val contents: List<CartonContentDto> = emptyList(),
    @SerializedName("total") val total: Int = 0,
)

// ----------------------------------------------------------------- the rest --

data class DisplayedAtRequest(
    @SerializedName("displayed_at") val displayedAt: String,
)

data class AssistanceRequest(
    @SerializedName("operator_id") val operatorId: Int,
    @SerializedName("shipment_id") val shipmentId: Int?,
    @SerializedName("stage") val stage: String,
    @SerializedName("cause") val cause: String,
    @SerializedName("note") val note: String?,
)

data class SizeDto(
    @SerializedName("canonical") val canonical: String,
    @SerializedName("sort_order") val sortOrder: Int,
)

data class HealthDto(
    @SerializedName("status") val status: String,
    @SerializedName("time") val time: String?,
)
