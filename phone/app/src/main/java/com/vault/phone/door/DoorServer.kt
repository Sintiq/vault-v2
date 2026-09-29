package com.vault.phone.door

import android.content.Context
import fi.iki.elonen.NanoHTTPD
import org.json.JSONArray
import org.json.JSONObject

/**
 * The door itself.
 *
 * Bound to this phone's Tailscale address and nothing else, behind a key the
 * phone generated. Every answer is written to the phone's own receipt log
 * first, so a call that the PC later denies making is still on record here.
 *
 * The tier of each route is not a comment, it is the code path: the read tier
 * answers, the ask tier goes through Approval, and the never tier has no route.
 */
class DoorServer(
    private val context: Context,
    private val address: String,
    port: Int,
    private val key: DoorKey,
    private val receipts: DoorReceipts,
) : NanoHTTPD(address, port) {

    private val can = Capabilities(context)

    companion object {
        /**
         * The whole surface, in one place. This is the passport as code: a
         * route that is not listed here cannot be reached, and adding one
         * means editing the list on purpose rather than by accident.
         */
        const val TOOL_SCREEN = "/door/screenshot"
        const val TOOL_ACT = "/door/act"
        const val TOOL_ACT_END = "/door/act/end"
        const val TOOL_REMIND = "/door/remind"

        val TOOLS = setOf(
            "/door/status", "/door/battery", "/door/network", "/door/receipts",
            "/door/location", "/door/ring", "/door/ring/stop", "/door/launch",
            TOOL_SCREEN, TOOL_ACT, TOOL_ACT_END, TOOL_REMIND,
        )

        /** Constant time: a timing difference on a shared tailnet is still a leak. */
        fun keyMatches(presented: String, expected: String): Boolean {
            if (expected.isEmpty() || presented.length != expected.length) return false
            var diff = 0
            for (i in expected.indices) diff = diff or (presented[i].code xor expected[i].code)
            return diff == 0
        }
    }

    private fun json(obj: JSONObject, status: Response.Status = Response.Status.OK): Response =
        newFixedLengthResponse(status, "application/json", obj.toString())

    private fun error(message: String, status: Response.Status): Response =
        json(JSONObject().put("error", message), status)

    private fun authorised(session: IHTTPSession): Boolean {
        val header = session.headers["authorization"].orEmpty()
        val presented = if (header.startsWith("Bearer ", true)) header.substring(7).trim() else ""
        return keyMatches(presented, key.value)
    }

    override fun serve(session: IHTTPSession): Response {
        val path = session.uri.trimEnd('/')
        // Bind before authentication: a reopened window cannot revive an older request.
        val inputGeneration = if (path == TOOL_ACT) InputSession.requestGeneration() else null

        if (path == "/door/ping") {
            return json(JSONObject().put("ok", true).put("device", android.os.Build.MODEL))
        }
        // Anything not in the passport is refused before the key is even read,
        // so an unknown tool cannot be probed for a timing difference either.
        if (path !in TOOLS) {
            return error("no such tool", Response.Status.NOT_FOUND)
        }
        if (!key.isOpen) {
            return error("the door is closed on the phone", Response.Status.FORBIDDEN)
        }
        if (!authorised(session)) {
            receipts.append(path, "-", "refused", "bad key")
            return error("bad key", Response.Status.UNAUTHORIZED)
        }

        return try {
            when (path) {
                "/door/status"   -> json(status())
                "/door/battery"  -> read("battery") { can.battery() }
                "/door/network"  -> read("network") { can.network() }
                "/door/receipts" -> json(receiptsPayload(session))
                "/door/location" -> location()
                "/door/ring"     -> act("ring") { can.ring() }
                "/door/ring/stop" -> act("ring.stop") { can.stopRinging() }
                "/door/launch"   -> launch(session)
                TOOL_SCREEN      -> screenshot()
                TOOL_ACT         -> useTheScreen(session, requireNotNull(inputGeneration))
                TOOL_ACT_END     -> stopUsingTheScreen()
                TOOL_REMIND      -> remind(session)
                else -> error("no such tool", Response.Status.NOT_FOUND)  // unreachable: TOOLS guards it
            }
        } catch (e: Exception) {
            receipts.append(path, "-", "failed", e.message.orEmpty())
            error(e.message ?: "failed", Response.Status.INTERNAL_ERROR)
        }
    }

    // -- tiers ---------------------------------------------------------------

    private fun read(tool: String, block: () -> JSONObject): Response {
        val result = block()
        receipts.append(tool, "read", "answered")
        return json(result)
    }

    private fun act(tool: String, block: () -> JSONObject): Response {
        val result = block()
        receipts.append(tool, "act", "done")
        return json(result)
    }

    /** The one place coordinates exist — and they are never written down. */
    private fun location(): Response {
        if (!Approval.canAsk(context)) {
            receipts.append("location", "owner-only", "refused", Approval.CANNOT_ASK)
            return error(Approval.CANNOT_ASK, Response.Status.FORBIDDEN)
        }
        val result = can.location()
        val refused = result.has("error")
        receipts.append(
            "location", "owner-only",
            if (refused) "refused" else "answered",
            // The passport: logged as requested by the owner, without coordinates.
            if (refused) result.optString("error") else "coordinates returned, not recorded",
        )
        return json(result, if (refused) Response.Status.FORBIDDEN else Response.Status.OK)
    }

    /**
     * A picture of the screen: asked for here, and asked for again by Android
     * itself. The bytes go straight out on this response — the phone keeps
     * nothing — and the log records that a picture was taken, not what of.
     */
    private fun screenshot(): Response {
        val result = Screenshot.ask(context)
        val png = result.png
        if (png == null) {
            val refusal = result.error ?: "refused on the phone"
            receipts.append("screenshot", "owner-only", "refused", "${result.requestId}: $refusal")
            return json(JSONObject().put("error", refusal).put("request_id", result.requestId),
                Response.Status.FORBIDDEN)
        }
        receipts.append("screenshot", "owner-only", "answered",
                        "${result.requestId}: ${png.size / 1024} KB sent, nothing kept")
        return newFixedLengthResponse(
            Response.Status.OK, "image/png", java.io.ByteArrayInputStream(png), png.size.toLong()
        ).also { it.addHeader("X-Request-ID", result.requestId) }
    }

    /**
     * A tap, a swipe, or back and home. Only inside a window the owner opened,
     * and the phone cannot read what it is tapping — see InputService.
     */
    private fun useTheScreen(session: IHTTPSession, inputGeneration: Long): Response {
        val body = HashMap<String, String>()
        session.parseBody(body)
        val payload = runCatching { JSONObject(body["postData"].orEmpty()) }.getOrDefault(JSONObject())
        val what = payload.optString("do").trim().lowercase()

        val refusal = InputSession.ensureOpen(context, inputGeneration)
        if (refusal != null) {
            receipts.append("act", "owner-only", "refused", refusal)
            return error(refusal, Response.Status.FORBIDDEN)
        }
        val finger = InputService.current()
            ?: return error(InputService.NOT_ENABLED, Response.Status.FORBIDDEN).also {
                receipts.append("act", "owner-only", "refused", InputService.NOT_ENABLED)
            }

        if (what !in setOf("tap", "swipe", "back", "home", "recents")) {
            return error("tap, swipe, back, home or recents", Response.Status.BAD_REQUEST)
        }
        val done = InputSession.actIfOpen(inputGeneration) { when (what) {
            "tap" -> finger.tap(
                payload.optDouble("x", -1.0).toFloat(), payload.optDouble("y", -1.0).toFloat())
            "swipe" -> finger.swipe(
                payload.optDouble("x", -1.0).toFloat(), payload.optDouble("y", -1.0).toFloat(),
                payload.optDouble("toX", -1.0).toFloat(), payload.optDouble("toY", -1.0).toFloat(),
                payload.optLong("ms", 250))
            "back", "home", "recents" -> finger.press(what)
            else -> false
        } } ?: return error("no window open", Response.Status.FORBIDDEN).also {
            receipts.append("act", "owner-only", "refused", "no window open")
        }
        // Where it tapped is the whole audit trail; without it the log says nothing.
        receipts.append("act", "owner-only", if (done) "done" else "failed",
                        describe(what, payload))
        return json(JSONObject()
            .put("done", done)
            .put("seconds_left", InputSession.secondsLeft)
            .put("actions_this_window", InputSession.actionsThisWindow))
    }

    private fun describe(what: String, payload: JSONObject): String = when (what) {
        "tap" -> "tap at ${payload.optInt("x")},${payload.optInt("y")}"
        "swipe" -> "swipe ${payload.optInt("x")},${payload.optInt("y")} to " +
            "${payload.optInt("toX")},${payload.optInt("toY")}"
        else -> what
    }

    private fun stopUsingTheScreen(): Response {
        InputSession.close()
        receipts.append("act.end", "owner-only", "done", "window closed")
        return json(JSONObject().put("open", false))
    }

    /**
     * The vault says which tasks are due; the phone shows one notification.
     * Tapping it opens the Tasks tab. Titles are the owner's own task names,
     * shown only to him on his own phone; the log records the count.
     */
    private fun remind(session: IHTTPSession): Response {
        val body = HashMap<String, String>()
        session.parseBody(body)
        val payload = runCatching { JSONObject(body["postData"].orEmpty()) }.getOrDefault(JSONObject())
        val items = payload.optJSONArray("items") ?: org.json.JSONArray()
        if (items.length() == 0) return error("nothing to remind about", Response.Status.BAD_REQUEST)
        if (!Approval.canAsk(context)) {
            receipts.append("remind", "act", "refused", Approval.CANNOT_ASK)
            return error(Approval.CANNOT_ASK, Response.Status.FORBIDDEN)
        }
        val lines = (0 until items.length()).map { i ->
            val o = items.getJSONObject(i)
            o.optString("title") + " — " + o.optString("when")
        }
        DoorNotifications.reminder(context, lines)
        receipts.append("remind", "act", "shown", "${lines.size} task(s) due")
        return json(JSONObject().put("shown", true).put("count", lines.size))
    }

    private fun launch(session: IHTTPSession): Response {
        val body = HashMap<String, String>()
        session.parseBody(body)
        val payload = runCatching { JSONObject(body["postData"].orEmpty()) }.getOrDefault(JSONObject())
        val packageName = payload.optString("package").trim()
        if (packageName.isEmpty()) return error("which app?", Response.Status.BAD_REQUEST)
        val result = can.launch(packageName)
        val outcome = when {
            result.has("error") -> "failed"
            result.optBoolean("opened") -> "opened"
            else -> "asked"
        }
        receipts.append("launch", "act", outcome, packageName)
        return json(result)
    }

    // -- status and log ------------------------------------------------------

    private fun status(): JSONObject = JSONObject()
        .put("device", android.os.Build.MODEL)
        .put("address", address)
        .put("open", key.isOpen)
        .put("receipts", receipts.verify())
        .put("chain_intact", receipts.verify() >= 0)

    private fun receiptsPayload(session: IHTTPSession): JSONObject {
        val n = session.parameters["n"]?.firstOrNull()?.toIntOrNull() ?: 60
        val rows = JSONArray()
        receipts.tail(n.coerceIn(1, 500)).forEach { rows.put(it) }
        receipts.append("receipts", "read", "answered")
        return JSONObject().put("intact", receipts.verify() >= 0).put("rows", rows)
    }
}
