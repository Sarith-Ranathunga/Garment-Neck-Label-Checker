package lk.ac.nibm.gnlc.net

import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part
import retrofit2.http.Path
import retrofit2.http.Query

/**
 * The host's HTTP interface, as the phone uses it.
 *
 * Every function is `suspend`. There is no synchronous variant, because a
 * blocking call on the main thread here would be a frozen camera screen in
 * front of an operator holding a garment.
 */
interface Api {

    @GET("health")
    suspend fun health(): HealthDto

    @POST("api/login")
    suspend fun login(@Body body: LoginRequest): UserDto

    @GET("api/operators")
    suspend fun operators(): List<UserDto>

    @GET("api/sizes")
    suspend fun sizes(): List<SizeDto>

    @GET("api/checklists")
    suspend fun checklists(@Query("stage") stage: String? = null): List<ChecklistDto>

    @GET("api/checklist/{id}/bundle")
    suspend fun bundle(@Path("id") checklistId: Int): ChecklistBundleDto

    /**
     * One scan.
     *
     * `payload` is the JSON half and `image` the neck-label photograph. The
     * hangtag barcode is sent as text, already decoded on the device by
     * ML Kit — only the neck label needs the server, because only the neck
     * label needs OCR.
     */
    @Multipart
    @POST("api/scan")
    suspend fun scan(
        @Part("payload") payload: RequestBody,
        @Part image: MultipartBody.Part?,
    ): ScanResponseDto

    @POST("api/manual-entry")
    suspend fun manualEntry(@Body body: ManualEntryRequest): Map<String, Any?>

    @POST("api/bypass")
    suspend fun bypass(@Body body: BypassRequest): BypassResponseDto

    @POST("api/scan/{id}/displayed")
    suspend fun markDisplayed(
        @Path("id") scanEventId: Long,
        @Body body: DisplayedAtRequest,
    ): Map<String, Any?>

    @POST("api/carton")
    suspend fun openCarton(@Body body: CartonOpenRequest): CartonDto

    @POST("api/carton/{id}/close")
    suspend fun closeCarton(
        @Path("id") cartonId: Int,
        @Body body: CartonCloseRequest,
    ): CartonCloseResponseDto

    @GET("api/shipment/{id}/cartons")
    suspend fun cartons(
        @Path("id") shipmentId: Int,
        @Query("open_only") openOnly: Boolean = false,
    ): List<CartonDto>

    @POST("api/assistance")
    suspend fun assistance(@Body body: AssistanceRequest): Map<String, Any?>
}
