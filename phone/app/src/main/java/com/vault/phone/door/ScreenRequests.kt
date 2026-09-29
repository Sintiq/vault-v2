package com.vault.phone.door

import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** One owner-approved capture at a time; every transition names the same request. */
class ScreenRequests(private val now: () -> Long = { System.nanoTime() / 1_000_000 }) {
    companion object {
        const val WINDOW_MS = 60_000L
        const val ONE_AT_A_TIME = "one at a time"
    }

    class Request internal constructor(val id: String, val accepted: Boolean, internal val deadline: Long) {
        internal val done = CountDownLatch(1)
        internal var phase = "waiting"
        internal var png: ByteArray? = null
        internal var error: String? = null
        internal var release: (() -> Unit)? = null
        internal var consumed = false
    }

    data class Result(val requestId: String, val png: ByteArray?, val error: String?)

    private val lock = Any()
    private var current: Request? = null

    fun begin(): Request = synchronized(lock) {
        expire()
        val request = Request(UUID.randomUUID().toString(), current == null, now() + WINDOW_MS)
        if (request.accepted) current = request
        else complete(request, null, ONE_AT_A_TIME)
        request
    }

    fun consent(id: String): Boolean = synchronized(lock) {
        val request = matching(id) ?: return@synchronized false
        if (request.phase != "waiting") return@synchronized false
        request.phase = "consent"
        true
    }

    fun awaitingCapture(id: String): Boolean = synchronized(lock) {
        matching(id)?.phase == "consent"
    }

    /** release must schedule nonwaiting, request-local resource cleanup. */
    fun capture(id: String, release: () -> Unit): Boolean = synchronized(lock) {
        val request = matching(id) ?: return@synchronized false
        if (request.phase != "consent") return@synchronized false
        request.phase = "capture"
        request.release = release
        true
    }

    fun deliver(id: String, png: ByteArray?): Boolean = synchronized(lock) {
        val request = matching(id) ?: return@synchronized false
        if (request.phase != "capture") return@synchronized false
        complete(request, png, if (png == null) "refused on the phone" else null)
        true
    }

    fun deny(id: String): Boolean = synchronized(lock) {
        val request = matching(id) ?: return@synchronized false
        complete(request, null, "refused on the phone")
        true
    }

    fun cancelAll() = synchronized(lock) {
        current?.let { complete(it, null, "refused on the phone") }
    }

    fun await(request: Request): Result {
        try {
            request.done.await(maxOf(0, request.deadline - now()), TimeUnit.MILLISECONDS)
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
        }
        return synchronized(lock) {
            if (request.phase != "done") complete(request, null, "refused on the phone")
            if (request.consumed) return@synchronized Result(request.id, null, "request already completed")
            request.consumed = true
            Result(request.id, request.png, request.error).also { request.png = null }
        }
    }

    private fun matching(id: String): Request? {
        expire()
        return current?.takeIf { it.id == id }
    }

    private fun expire() {
        current?.takeIf { now() >= it.deadline }?.let { complete(it, null, "refused on the phone") }
    }

    private fun complete(request: Request, png: ByteArray?, error: String?) {
        if (request.phase == "done") return
        request.phase = "done"
        request.png = png
        request.error = error
        if (current === request) current = null
        val release = request.release
        request.release = null
        runCatching { release?.invoke() }
        request.done.countDown()
    }
}
