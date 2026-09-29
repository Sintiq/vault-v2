package com.vault.phone

import android.content.Context
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.PermissionController
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.OxygenSaturationRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlin.reflect.KClass

/**
 * What the watch writes into Health Connect, read on the phone.
 *
 * Samsung Health copies the watch's readings into Health Connect; this only
 * reads them back — never writes, never stores them, never sends them
 * anywhere. The numbers live on the screen while the tab is open and nowhere
 * else in this app.
 */
class HealthData(private val context: Context) {

    /** Today's picture, or why there is none. Every field may be absent. */
    data class Today(
        val steps: Long?,
        val heartMin: Long?, val heartAvg: Long?, val heartMax: Long?,
        val lastHeart: Long?, val lastHeartAt: Instant?,
        val sleep: Duration?, val sleepEnded: Instant?,
        val oxygen: Double?, val oxygenAt: Instant?,
    )

    sealed interface State {
        data object NotInstalled : State
        data object NeedsUpdate : State
        data object NeedsPermission : State
        data class Ready(val today: Today) : State
        data class Failed(val why: String) : State
    }

    companion object {
        const val PROVIDER = "com.google.android.apps.healthdata"
        /** Pages of records; a day past MAX_PAGES of them is not described from a fragment. */
        const val PAGE = 1000
        const val MAX_PAGES = 20

        val PERMISSIONS: Set<String> = setOf(
            HealthPermission.getReadPermission(StepsRecord::class),
            HealthPermission.getReadPermission(HeartRateRecord::class),
            HealthPermission.getReadPermission(SleepSessionRecord::class),
            HealthPermission.getReadPermission(OxygenSaturationRecord::class),
        )

        /** The contract that shows Health Connect's own permission screen. */
        fun permissionContract() = PermissionController.createRequestPermissionResultContract(PROVIDER)
    }

    private fun availability(): Int = HealthConnectClient.getSdkStatus(context, PROVIDER)

    suspend fun read(zone: ZoneId = ZoneId.systemDefault(), now: Instant = Instant.now()): State {
        when (availability()) {
            HealthConnectClient.SDK_UNAVAILABLE -> return State.NotInstalled
            HealthConnectClient.SDK_UNAVAILABLE_PROVIDER_UPDATE_REQUIRED -> return State.NeedsUpdate
        }
        return try {
            val client = HealthConnectClient.getOrCreate(context, PROVIDER)
            val granted = client.permissionController.getGrantedPermissions()
            if (!granted.containsAll(PERMISSIONS)) return State.NeedsPermission
            State.Ready(today(client, zone, now))
        } catch (e: Exception) {
            State.Failed(e.message ?: "Health Connect did not answer")
        }
    }

    /** One calendar day for the timeline, or the reason it could not be read. */
    sealed interface Day {
        data class Ready(val readings: WatchReadings) : Day
        data class Unavailable(val why: String) : Day
    }

    suspend fun day(day: LocalDate, zone: ZoneId = ZoneId.systemDefault()): Day {
        when (availability()) {
            HealthConnectClient.SDK_UNAVAILABLE -> return Day.Unavailable("Health Connect is not installed")
            HealthConnectClient.SDK_UNAVAILABLE_PROVIDER_UPDATE_REQUIRED -> return Day.Unavailable("Health Connect needs an update")
        }
        return try {
            val client = HealthConnectClient.getOrCreate(context, PROVIDER)
            if (!client.permissionController.getGrantedPermissions().containsAll(PERMISSIONS)) {
                return Day.Unavailable("Vault may not read Health Connect yet")
            }
            Day.Ready(calendarDay(client, zone, day))
        } catch (e: Exception) {
            Day.Unavailable(e.message ?: "Health Connect did not answer")
        }
    }

    /**
     * Every record of a kind in the window, page after page. The writers are
     * named from these, so a day too big to read whole is refused rather than
     * described from its first page.
     */
    private suspend fun <T : Record> allRecords(client: HealthConnectClient, type: KClass<T>,
                                                filter: TimeRangeFilter): List<T> {
        val out = ArrayList<T>()
        var token: String? = null
        var pages = 0
        do {
            val page = client.readRecords(ReadRecordsRequest(type, filter, pageSize = PAGE, pageToken = token))
            out += page.records
            // A provider that hands back the same token, or a page with nothing on it,
            // has nothing more to say; treat either as the end rather than looping.
            if (page.records.isEmpty() || page.pageToken == token) break
            token = page.pageToken
            if (++pages > MAX_PAGES && token != null) {
                throw IllegalStateException("${type.simpleName}: ${out.size} records over $pages pages and more — not read whole")
            }
        } while (token != null)
        return out
    }

    /**
     * The day as a calendar day, [midnight, next midnight): steps and heart rate
     * inside it, the last oxygen reading inside it, and the sleep that ended in
     * it — a night belongs to the morning it ended on. Each metric carries the
     * device and app Health Connect recorded as its writer.
     */
    private suspend fun calendarDay(client: HealthConnectClient, zone: ZoneId, day: LocalDate): WatchReadings {
        val start = day.atStartOfDay(zone).toInstant()
        val end = day.plusDays(1).atStartOfDay(zone).toInstant()
        val window = TimeRangeFilter.between(start, end)

        val totals = client.aggregate(
            AggregateRequest(
                metrics = setOf(StepsRecord.COUNT_TOTAL, HeartRateRecord.BPM_MIN,
                                HeartRateRecord.BPM_AVG, HeartRateRecord.BPM_MAX),
                timeRangeFilter = window,
            )
        )
        val steps = allRecords(client, StepsRecord::class, window)
        val heart = allRecords(client, HeartRateRecord::class, window)
        val sleep = allRecords(client, SleepSessionRecord::class,
                               TimeRangeFilter.between(start.minus(Duration.ofHours(24)), end))
            .filter { !it.endTime.isBefore(start) && it.endTime.isBefore(end) }
        val oxygen = client.readRecords(
            ReadRecordsRequest(OxygenSaturationRecord::class, window, ascendingOrder = false, pageSize = 1)
        ).records.firstOrNull()

        val sleepTotal = WatchDays.slept(sleep.map { it.startTime to it.endTime })
        return WatchReadings(
            steps = totals[StepsRecord.COUNT_TOTAL],
            heart_min = totals[HeartRateRecord.BPM_MIN], heart_avg = totals[HeartRateRecord.BPM_AVG],
            heart_max = totals[HeartRateRecord.BPM_MAX],
            sleep_minutes = sleepTotal?.toMinutes(),
            sleep_end = sleep.maxOfOrNull { it.endTime }?.let { WatchDays.clock(it, zone) },
            oxygen = oxygen?.percentage?.value,
            steps_source = WatchDays.source(steps.map { it.metadata }),
            heart_source = WatchDays.source(heart.map { it.metadata }),
            sleep_source = WatchDays.source(sleep.map { it.metadata }),
            oxygen_source = oxygen?.let { WatchDays.source(listOf(it.metadata)) },
        )
    }

    private suspend fun today(client: HealthConnectClient, zone: ZoneId, now: Instant): Today {
        val midnight = LocalDate.ofInstant(now, zone).atStartOfDay(zone).toInstant()
        val sinceMidnight = TimeRangeFilter.between(midnight, now)

        // Aggregates, not a sum of records: Health Connect removes the
        // overlap when both the phone and the watch counted the same steps.
        val totals = client.aggregate(
            AggregateRequest(
                metrics = setOf(StepsRecord.COUNT_TOTAL, HeartRateRecord.BPM_MIN,
                                HeartRateRecord.BPM_AVG, HeartRateRecord.BPM_MAX),
                timeRangeFilter = sinceMidnight,
            )
        )

        val lastHeart = client.readRecords(
            ReadRecordsRequest(HeartRateRecord::class, TimeRangeFilter.between(now.minus(Duration.ofHours(6)), now),
                               ascendingOrder = false, pageSize = 1)
        ).records.firstOrNull()?.samples?.maxByOrNull { it.time }

        // Last night: sessions that ended in the past 18 hours.
        val sleep = client.readRecords(
            ReadRecordsRequest(SleepSessionRecord::class, TimeRangeFilter.between(now.minus(Duration.ofHours(18)), now))
        ).records
        val sleepTotal = WatchDays.slept(sleep.map { it.startTime to it.endTime })

        val oxygen = client.readRecords(
            ReadRecordsRequest(OxygenSaturationRecord::class, TimeRangeFilter.between(now.minus(Duration.ofDays(1)), now),
                               ascendingOrder = false, pageSize = 1)
        ).records.firstOrNull()

        return Today(
            steps = totals[StepsRecord.COUNT_TOTAL],
            heartMin = totals[HeartRateRecord.BPM_MIN], heartAvg = totals[HeartRateRecord.BPM_AVG],
            heartMax = totals[HeartRateRecord.BPM_MAX],
            lastHeart = lastHeart?.beatsPerMinute, lastHeartAt = lastHeart?.time,
            sleep = sleepTotal, sleepEnded = sleep.maxOfOrNull { it.endTime },
            oxygen = oxygen?.percentage?.value, oxygenAt = oxygen?.time,
        )
    }
}

/** Naming the writer of a reading, in words. Pure, so it is tested without a phone. */
object WatchDays {
    private val hhmm: DateTimeFormatter = DateTimeFormatter.ofPattern("HH:mm")

    fun clock(at: Instant, zone: ZoneId): String = hhmm.withZone(zone).format(at)

    /**
     * Time asleep as the union of the sessions, not their sum: Samsung Health can
     * hand the same night to Health Connect twice, and two copies of one night are
     * still one night. A nap that does not overlap still adds. Null without sessions.
     */
    fun slept(sessions: List<Pair<Instant, Instant>>): Duration? {
        if (sessions.isEmpty()) return null
        var total = Duration.ZERO
        var start: Instant? = null
        var end: Instant? = null
        for ((s, e) in sessions.sortedBy { it.first }) {
            if (end != null && !s.isAfter(end)) {
                if (e.isAfter(end)) end = e
            } else {
                if (start != null) total += Duration.between(start, end)
                start = s; end = e
            }
        }
        if (start != null) total += Duration.between(start, end)
        return total
    }

    /** The apps Health Connect knows by package name; anything else is shown as the package. */
    private val APPS = mapOf(
        "com.sec.android.app.shealth" to "Samsung Health",
        "com.google.android.apps.healthdata" to "Health Connect",
        "com.google.android.apps.fitness" to "Google Fit",
        "com.vault.phone" to "Vault",
    )

    fun source(metadata: List<Metadata>): String? = source(
        metadata.mapNotNull { it.device?.model?.takeIf { m -> m.isNotBlank() } },
        metadata.map { it.dataOrigin.packageName },
    )

    /**
     * "Galaxy Watch Ultra via Samsung Health" when a device was recorded, "Samsung Health
     * (device not named)" when only the app was, null when nothing was — the PC then
     * writes "source not verified" rather than a watch the data never mentioned.
     */
    fun source(deviceModels: List<String>, packages: List<String>): String? {
        val devices = deviceModels.distinct().sorted()
        val apps = packages.distinct().map { APPS[it] ?: it }.distinct().sorted()
        if (devices.isEmpty() && apps.isEmpty()) return null
        val via = if (apps.isEmpty()) "" else " via " + apps.joinToString(", ")
        return if (devices.isEmpty()) apps.joinToString(", ") + " (device not named)"
               else devices.joinToString(", ") + via
    }
}

/** Words for the numbers. Pure, so it is tested without a phone. */
object HealthText {
    fun duration(d: Duration?): String? = d?.let {
        val h = it.toHours(); val m = it.toMinutesPart()
        if (h > 0) "${h} h ${m} min" else "${m} min"
    }

    fun ago(then: Instant?, now: Instant = Instant.now()): String? = then?.let {
        val minutes = Duration.between(it, now).toMinutes()
        when {
            minutes < 1 -> "just now"
            minutes < 60 -> "$minutes min ago"
            minutes < 24 * 60 -> "${minutes / 60} h ago"
            else -> "${minutes / (24 * 60)} d ago"
        }
    }

    fun heart(min: Long?, avg: Long?, max: Long?): String? =
        if (min == null || max == null) null else "today ${min}–${max} bpm" + (avg?.let { ", average $it" } ?: "")

    fun oxygen(value: Double?): String? = value?.let { String.format(java.util.Locale.US, "%.0f%%", it) }
}
