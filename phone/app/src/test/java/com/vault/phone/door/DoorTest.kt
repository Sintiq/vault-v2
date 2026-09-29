package com.vault.phone.door

import java.io.File
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * The door guards someone's phone, so the parts that decide who gets in are
 * tested rather than trusted. Each test states the rule it is defending.
 */
class DoorTest {

    @get:Rule
    val folder = TemporaryFolder()

    private fun receipts() = DoorReceipts(File(folder.newFolder(), "log.jsonl"))

    // -- the log ------------------------------------------------------------

    @Test
    fun `a knock is recorded and the chain verifies`() {
        val log = receipts()
        log.append("battery", "read", "answered")
        log.append("location", "owner-only", "refused", "refused on the phone")
        assertEquals(2, log.verify())
        assertEquals("location", log.tail(1).single().getString("tool"))
    }

    @Test
    fun `the first record chains to genesis, each one to the last`() {
        val log = receipts()
        log.append("a", "read", "answered")
        log.append("b", "read", "answered")
        val rows = log.readLines()
        assertEquals(DoorReceipts.GENESIS, rows[0].getString("prev"))
        assertEquals(rows[0].getString("hash"), rows[1].getString("prev"))
    }

    @Test
    fun `an edited record breaks the chain`() {
        val file = File(folder.newFolder(), "log.jsonl")
        val log = DoorReceipts(file)
        log.append("battery", "read", "answered")
        log.append("ring", "act", "done")
        log.append("network", "read", "answered")
        assertEquals(3, log.verify())

        // Someone rewrites the middle line to hide that the phone was rung.
        val lines = file.readLines().toMutableList()
        lines[1] = JSONObject(lines[1]).put("outcome", "refused").toString()
        file.writeText(lines.joinToString("\n") + "\n")

        assertEquals("a tampered record must be detectable", -1, log.verify())
    }

    @Test
    fun `a removed record breaks the chain`() {
        val file = File(folder.newFolder(), "log.jsonl")
        val log = DoorReceipts(file)
        repeat(3) { log.append("battery", "read", "answered") }
        val lines = file.readLines()
        file.writeText(listOf(lines[0], lines[2]).joinToString("\n") + "\n")
        assertEquals("a deleted middle record must be detectable", -1, log.verify())
    }

    @Test
    fun `a location entry never carries coordinates`() {
        val file = File(folder.newFolder(), "log.jsonl")
        val log = DoorReceipts(file)
        // Exactly what DoorServer writes for the owner-only tier.
        log.append("location", "owner-only", "answered", "coordinates returned, not recorded")
        val written = file.readText().lowercase()
        listOf("latitude", "longitude", "accuracy", "37.", "-122.").forEach {
            assertFalse("the log must not record where he was: $it", written.contains(it))
        }
    }

    @Test
    fun `rotation preserves historical bytes and chains the next record`() {
        val file = File(folder.newFolder(), "log.jsonl")
        val log = DoorReceipts(file)
        log.append("battery", "read", "answered", "x".repeat(1024 * 1024))
        val original = file.readBytes()
        val previousHash = log.readLines().last().getString("hash")

        log.trim()

        val segment = File(file.parentFile, "log.1.jsonl")
        assertTrue("rotation keeps the old segment", segment.isFile)
        assertTrue("historical bytes are never rewritten", original.contentEquals(segment.readBytes()))
        assertEquals(1, log.verify())
        log.append("network", "read", "answered")
        assertEquals(previousHash, JSONObject(file.readLines().single()).getString("prev"))
        assertEquals(2, log.verify())
        assertEquals(listOf("battery", "network"), log.readLines().map { it.getString("tool") })
    }

    @Test
    fun `malformed receipt lines remain visible and break verification`() {
        val file = File(folder.newFolder(), "log.jsonl")
        val log = DoorReceipts(file)
        log.append("battery", "read", "answered")
        file.appendText("not JSON\n")

        assertEquals(-1, log.verify())
        assertEquals(2, log.readLines().size)
        assertTrue("the displayed diagnostic is not a receipt", log.tail(1).single().getBoolean("receipt_error"))
        assertEquals("BROKEN", log.tail(1).single().getString("outcome"))
    }

    @Test
    fun `malformed old segment is not skipped after rotation`() {
        val file = File(folder.newFolder(), "log.jsonl")
        val log = DoorReceipts(file, maxBytes = 1)
        log.append("battery", "read", "answered")
        log.append("network", "read", "answered")
        val archive = File(file.parentFile, "log.1.jsonl")
        archive.appendText("\n")
        val historicalBytes = archive.readBytes()

        assertEquals(-1, log.verify())
        assertTrue(log.readLines().any { it.optBoolean("receipt_error") })
        log.trim()
        assertEquals(-1, log.verify())
        assertTrue(historicalBytes.contentEquals(archive.readBytes()))
    }

    @Test
    fun `invalid archive indices are visible instead of ignored`() {
        listOf("log.0.jsonl", "log.01.jsonl", "log.bad.jsonl", "log.999999999999999999999999999.jsonl").forEach { name ->
            val file = File(folder.newFolder(), "log.jsonl")
            val log = DoorReceipts(file)
            log.append("battery", "read", "answered")
            File(file.parentFile, name).writeBytes(file.readBytes())

            assertEquals("invalid segment $name must not disappear", -1, log.verify())
            assertTrue(log.readLines().any { it.optBoolean("receipt_error") })
        }
    }

    @Test
    fun `an unreadable archive is reported as broken rather than crashing`() {
        val file = File(folder.newFolder(), "log.jsonl")
        val log = DoorReceipts(file)
        log.append("battery", "read", "answered")
        assertTrue(File(file.parentFile, "log.1.jsonl").mkdir())

        assertEquals(-1, log.verify())
        assertTrue(log.readLines().any { it.optBoolean("receipt_error") })
    }

    @Test
    fun `diagnostic receipts display no invented timestamp`() {
        assertEquals("time unavailable", receiptTime(JSONObject()))
        assertEquals("time unavailable", receiptTime(JSONObject().put("ts", "not a timestamp")))
        assertEquals("time unavailable", receiptTime(JSONObject().put("receipt_error", true).put("ts", 123456789L)))
        assertFalse(receiptTime(JSONObject().put("ts", 123456789L)) == "time unavailable")
    }

    @Test
    fun `many rotations survive reopening and old tampering still breaks the chain`() {
        val file = File(folder.newFolder(), "log.jsonl")
        val log = DoorReceipts(file, maxBytes = 1)
        repeat(12) { index -> log.append("tool-$index", "read", "answered") }
        log.trim()
        val saved = (1..12).associateWith { index -> File(file.parentFile, "log.$index.jsonl").readBytes() }
        assertEquals(12, log.verify())

        val reopened = DoorReceipts(file, maxBytes = 1)
        reopened.append("after-reopen", "read", "answered")
        assertEquals(13, reopened.verify())
        assertEquals((0..11).map { "tool-$it" } + "after-reopen", reopened.readLines().map { it.getString("tool") })
        saved.forEach { (index, bytes) ->
            assertTrue("rotation rewrote segment $index", bytes.contentEquals(File(file.parentFile, "log.$index.jsonl").readBytes()))
        }

        val old = File(file.parentFile, "log.6.jsonl")
        old.writeText(JSONObject(old.readText()).put("outcome", "tampered").toString() + "\n")
        assertEquals(-1, DoorReceipts(file).verify())
    }

    @Test
    fun `missing oldest or interior segments remain visibly broken`() {
        listOf(1, 2).forEach { missing ->
            val file = File(folder.newFolder(), "log.jsonl")
            val log = DoorReceipts(file, maxBytes = 1)
            repeat(3) { log.append("battery", "read", "answered") }
            log.trim()
            assertTrue(File(file.parentFile, "log.$missing.jsonl").delete())

            assertEquals(-1, DoorReceipts(file).verify())
            assertTrue(log.readLines().any { it.optBoolean("receipt_error") })
        }
    }

    @Test
    fun `two receipt instances serialize appends through rotations`() {
        val directory = folder.newFolder()
        val first = DoorReceipts(File(directory, "log.jsonl"), maxBytes = 400)
        val second = DoorReceipts(File(directory, "./log.jsonl"), maxBytes = 400)
        val startGate = java.util.concurrent.CountDownLatch(1)
        val errors = java.util.Collections.synchronizedList(mutableListOf<Throwable>())
        val workers = listOf(first, second).mapIndexed { index, log ->
            Thread {
                try {
                    startGate.await()
                    repeat(10) { log.append("$index-$it", "read", "answered") }
                } catch (error: Throwable) {
                    errors.add(error)
                }
            }.apply { start() }
        }
        startGate.countDown()
        workers.forEach { it.join(5_000) }

        assertFalse("receipt writer deadlocked", workers.any { it.isAlive })
        assertTrue(errors.toString(), errors.isEmpty())
        assertEquals(20, first.verify())
        assertEquals(20, second.readLines().map { it.getString("tool") }.toSet().size)
    }

    @Test
    fun `trailing junk or a second object cannot hide inside a receipt line`() {
        listOf(" trailing junk", " {}").forEach { suffix ->
            val file = File(folder.newFolder(), "log.jsonl")
            val log = DoorReceipts(file)
            log.append("battery", "read", "answered")
            file.writeText(file.readText().trimEnd() + suffix + "\n")

            assertEquals(-1, log.verify())
            assertTrue(log.readLines().single().getBoolean("receipt_error"))
        }
    }

    // -- the key ------------------------------------------------------------

    @Test
    fun `only the exact key opens the door`() {
        val key = "cvOsojnDb7uLODrLAGwdYpmW"
        assertTrue(DoorServer.keyMatches(key, key))
        assertFalse(DoorServer.keyMatches("", key))
        assertFalse(DoorServer.keyMatches(key.dropLast(1), key))
        assertFalse(DoorServer.keyMatches(key + "x", key))
        assertFalse(DoorServer.keyMatches(key.replaceFirst('c', 'C'), key))
        assertFalse("a prefix must not be enough", DoorServer.keyMatches("cv", key))
    }

    @Test
    fun `an empty expected key opens nothing`() {
        assertFalse("an unset key must never match", DoorServer.keyMatches("", ""))
        assertFalse(DoorServer.keyMatches("anything", ""))
    }

    // -- the surface --------------------------------------------------------

    @Test
    fun `the routes are exactly the passport, and nothing from the never list exists`() {
        assertEquals(
            setOf(
                "/door/status", "/door/battery", "/door/network", "/door/receipts",
                "/door/location", "/door/ring", "/door/ring/stop", "/door/launch",
                "/door/screenshot", "/door/act", "/door/act/end", "/door/remind",
            ),
            DoorServer.TOOLS,
        )
        listOf("sms", "message", "chat", "mail", "contact", "call", "bank", "install")
            .forEach { forbidden ->
                assertTrue(
                    "the never tier must have no route: $forbidden",
                    DoorServer.TOOLS.none { it.contains(forbidden, ignoreCase = true) },
                )
            }
    }

    // -- where the door is willing to listen --------------------------------

    @Test
    fun `only the tailnet range counts as a Tailscale address`() {
        listOf("100.64.0.2", "100.64.0.1", "100.127.255.254")
            .forEach { assertTrue(it, Capabilities.isTailscaleAddress(it)) }
        // 100.63 and 100.128 are outside 100.64.0.0/10 and are ordinary
        // internet addresses; binding there would expose the door.
        listOf("100.63.0.1", "100.128.0.1", "10.0.0.176", "192.168.1.5",
               "100.0.0.1", "1100.64.0.1", "100.64", "", "not.an.address.x")
            .forEach { assertFalse(it, Capabilities.isTailscaleAddress(it)) }
    }

    // -- using the screen ---------------------------------------------------

    @Test
    fun `a screenshot requires matching one use consent and capture`() {
        val requests = ScreenRequests()
        val request = requests.begin()
        assertTrue(request.accepted)
        assertFalse(requests.consent("foreign-request"))
        assertFalse(requests.capture(request.id) { })
        assertFalse(requests.deliver(request.id, byteArrayOf(9)))
        assertTrue(requests.consent(request.id))
        assertFalse(requests.consent(request.id))
        var released = 0
        assertTrue(requests.capture(request.id) { released++ })
        assertFalse(requests.capture(request.id) { })
        assertTrue(requests.deliver(request.id, byteArrayOf(1, 2, 3)))
        assertFalse(requests.deliver(request.id, byteArrayOf(4)))
        val result = requests.await(request)
        assertEquals(request.id, result.requestId)
        assertNull(result.error)
        assertArrayEquals(byteArrayOf(1, 2, 3), result.png)
        assertEquals(1, released)
    }

    @Test
    fun `a second screenshot is refused while the first waits`() {
        val requests = ScreenRequests()
        val first = requests.begin()
        val second = requests.begin()
        assertFalse(second.accepted)
        assertTrue(first.id != second.id)
        assertEquals("one at a time", requests.await(second).error)
        assertTrue(requests.consent(first.id))
        assertTrue(requests.capture(first.id) { })
        assertTrue(requests.deliver(first.id, byteArrayOf(1)))
        assertArrayEquals(byteArrayOf(1), requests.await(first).png)
    }

    @Test
    fun `sixty seconds expires consent and late frames cannot fill a newer request`() {
        var now = 1_000L
        val requests = ScreenRequests { now }
        val first = requests.begin()
        assertTrue(requests.consent(first.id))
        var oldReleased = 0
        assertTrue(requests.capture(first.id) { oldReleased++ })
        now += 60_000
        assertNull(requests.await(first).png)
        assertEquals(1, oldReleased)
        val second = requests.begin()
        assertTrue(second.accepted)
        assertFalse(requests.consent(first.id))
        assertFalse(requests.deliver(first.id, byteArrayOf(9)))
        assertFalse(requests.deny(first.id))
        assertTrue(requests.consent(second.id))
        var newReleased = 0
        assertTrue(requests.capture(second.id) { newReleased++ })
        assertFalse(requests.deliver(first.id, byteArrayOf(8)))
        assertEquals(0, newReleased)
        assertTrue(requests.deliver(second.id, byteArrayOf(2)))
        assertArrayEquals(byteArrayOf(2), requests.await(second).png)
        assertEquals(1, newReleased)
    }

    @Test
    fun `cancelled screenshot callbacks cannot reach a new request and results are one use`() {
        val requests = ScreenRequests()
        val first = requests.begin()
        assertTrue(requests.consent(first.id))
        var oldReleased = 0
        assertTrue(requests.capture(first.id) { oldReleased++ })
        requests.cancelAll()
        assertEquals(1, oldReleased)
        val second = requests.begin()
        assertTrue(second.accepted)
        assertFalse(requests.consent(first.id))
        assertFalse(requests.capture(first.id) { throw AssertionError("old cleanup") })
        assertFalse(requests.deliver(first.id, byteArrayOf(9)))
        assertFalse(requests.deny(first.id))
        assertNull(requests.await(first).png)
        assertTrue(requests.consent(second.id))
        var newReleased = 0
        assertTrue(requests.capture(second.id) { newReleased++ })
        assertTrue(requests.deliver(second.id, byteArrayOf(2)))
        assertArrayEquals(byteArrayOf(2), requests.await(second).png)
        assertEquals(1, newReleased)
        assertNull(requests.await(second).png)
        assertEquals(1, oldReleased)
    }

    @Test
    fun `late screenshot consent expires even before the waiting thread resumes`() {
        var now = 0L
        val requests = ScreenRequests { now }
        val first = requests.begin()
        now = 60_000L
        assertFalse(requests.consent(first.id))
        val second = requests.begin()
        assertTrue(second.accepted)
        assertFalse(requests.capture(first.id) { })
        assertNull(requests.await(first).png)
        assertTrue(requests.consent(second.id))
        assertTrue(requests.deny(second.id))
        assertNull(requests.await(second).png)
    }

    @Test
    fun `stopping the door closes input before the platform stop`() {
        InputSession.close()
        assertNull(InputSession.ensureOpen(inputEnabled = true, canAsk = true) { true })
        assertTrue(InputSession.isOpen)
        var stopped = false
        DoorService.stop {
            assertFalse(InputSession.isOpen)
            assertEquals(0L, InputSession.secondsLeft)
            stopped = true
        }
        assertTrue(stopped)
        assertFalse(InputSession.isOpen)
        assertEquals(0L, InputSession.secondsLeft)
    }

    @Test
    fun `rotating the key closes input before storing the new key`() {
        InputSession.close()
        assertNull(InputSession.ensureOpen(inputEnabled = true, canAsk = true) { true })
        var stored = ""
        val key = DoorKey.rotate {
            assertFalse(InputSession.isOpen)
            assertEquals(0L, InputSession.secondsLeft)
            stored = it
        }
        assertEquals(key, stored)
        assertEquals(24, key.length)
        assertFalse(InputSession.isOpen)
    }

    @Test
    fun `key rotation publishes the new key before admitting a new input generation`() {
        InputSession.close()
        val oldGeneration = InputSession.requestGeneration()
        val stored = java.util.concurrent.atomic.AtomicReference("old-key")
        val executor = java.util.concurrent.Executors.newSingleThreadExecutor()
        var incoming: java.util.concurrent.Future<Pair<Long, String>>? = null
        try {
            val replacement = DoorKey.rotate { fresh ->
                val entering = java.util.concurrent.CountDownLatch(1)
                incoming = executor.submit<Pair<Long, String>> {
                    entering.countDown()
                    InputSession.requestGeneration() to stored.get()
                }
                assertTrue(entering.await(2, java.util.concurrent.TimeUnit.SECONDS))
                try {
                    incoming!!.get(150, java.util.concurrent.TimeUnit.MILLISECONDS)
                    throw AssertionError("a new generation escaped while the old key was current")
                } catch (_: java.util.concurrent.TimeoutException) {
                    // Admission waits only for the nonwaiting key publication, never an owner prompt.
                }
                stored.set(fresh)
            }
            val admitted = incoming!!.get(2, java.util.concurrent.TimeUnit.SECONDS)
            assertTrue(admitted.first != oldGeneration)
            assertEquals(replacement, admitted.second)
            assertFalse(InputSession.isOpen)
        } finally {
            executor.shutdownNow()
            executor.awaitTermination(2, java.util.concurrent.TimeUnit.SECONDS)
            InputSession.close()
        }
    }

    @Test
    fun `failed key publication still leaves the input window closed`() {
        InputSession.close()
        val request = InputSession.requestGeneration()
        assertNull(InputSession.ensureOpen(true, true, request) { true })
        var failureSeen = false
        try {
            DoorKey.rotate { throw java.io.IOException("synthetic storage failure") }
        } catch (_: java.io.IOException) {
            failureSeen = true
        }
        assertTrue(failureSeen)
        assertFalse(InputSession.isOpen)
        assertEquals(0L, InputSession.secondsLeft)
        assertNull(InputSession.actIfOpen(request) { throw AssertionError("revoked input") })
    }

    @Test
    fun `late input approval after stop cannot reopen the window`() {
        InputSession.close()
        val refusal = InputSession.ensureOpen(inputEnabled = true, canAsk = true) {
            DoorService.stop { }
            true
        }
        assertEquals("no window open", refusal)
        assertFalse(InputSession.isOpen)
        assertEquals(0L, InputSession.secondsLeft)
    }

    @Test
    fun `closing after approval refuses final input dispatch`() {
        InputSession.close()
        val request = InputSession.requestGeneration()
        assertNull(InputSession.ensureOpen(true, true, request) { true })
        InputSession.close()
        var acted = false
        assertNull(InputSession.actIfOpen(request) { acted = true; true })
        assertFalse(acted)
        assertEquals(0L, InputSession.actionsThisWindow)
    }

    @Test
    fun `an old input request cannot borrow a reopened window`() {
        InputSession.close()
        val oldRequest = InputSession.requestGeneration()
        assertNull(InputSession.ensureOpen(true, true, oldRequest) { true })
        DoorKey.rotate { }
        val newRequest = InputSession.requestGeneration()
        assertNull(InputSession.ensureOpen(true, true, newRequest) { true })
        var oldActed = false
        assertNull(InputSession.actIfOpen(oldRequest) { oldActed = true; true })
        assertFalse(oldActed)
        var askedAgain = false
        assertEquals("no window open", InputSession.ensureOpen(true, true, oldRequest) {
            askedAgain = true
            true
        })
        assertFalse(askedAgain)
        assertEquals(true, InputSession.actIfOpen(newRequest) { true })
        assertEquals(1L, InputSession.actionsThisWindow)
        InputSession.close()
    }

    @Test
    fun `acting is closed until a window is opened, and closes again`() {
        InputSession.close()
        assertFalse("no window means no acting", InputSession.isOpen)
        assertEquals(0, InputSession.secondsLeft)
        assertEquals(0, InputSession.actionsThisWindow)
    }

    @Test
    fun `a window is minutes, not a state the phone is left in`() {
        // Long enough to do something; short enough that forgetting is not fatal.
        assertTrue(InputSession.WINDOW_MS in 60_000..15 * 60_000)
    }

    @Test
    fun `the service that taps asks Android for no screen contents`() {
        // The guarantee is in the manifest declaration, not in good intentions:
        // without canRetrieveWindowContent the platform will not hand it any.
        val config = java.io.File(
            "src/main/res/xml/input_service.xml"
        ).takeIf { it.exists() } ?: java.io.File("app/src/main/res/xml/input_service.xml")
        val text = config.readText()
        assertTrue(text.contains("""android:canRetrieveWindowContent="false""""))
        assertTrue(text.contains("""android:canPerformGestures="true""""))
        // Subscribing to nothing is done by saying nothing; there is no "none".
        assertFalse("must not subscribe to screen events",
                    text.contains("android:accessibilityEventTypes"))

        // And no code path from the service to a node tree.
        val source = java.io.File(
            "src/main/java/com/vault/phone/door/InputService.kt"
        ).takeIf { it.exists() } ?: java.io.File("app/src/main/java/com/vault/phone/door/InputService.kt")
        // Comments discuss what the file does not do, so strip the prose and
        // look only at what would actually run.
        val code = source.readText()
            .replace(Regex("""/\*.*?\*/""", RegexOption.DOT_MATCHES_ALL), "")
            .replace(Regex("""//.*"""), "")
        listOf("rootInActiveWindow", "findAccessibilityNodeInfos", "getChild", "getWindows")
            .forEach { assertFalse("must not read the screen: $it", code.contains(it)) }
    }

    // -- coming back after a restart ----------------------------------------

    @Test
    fun `waiting for Tailscale backs off, then gives up`() {
        // Early attempts are quick: at boot Tailscale is usually seconds away.
        assertTrue("the first wait must be short", StartupPolicy.delayMs(0) <= 3_000)
        // The gap widens rather than hammering.
        assertTrue(StartupPolicy.delayMs(3) > StartupPolicy.delayMs(1))
        // And it is capped, so a phone in a pocket is not polled forever.
        assertEquals(StartupPolicy.delayMs(5), StartupPolicy.delayMs(50))
        assertTrue(StartupPolicy.shouldKeepTrying(0))
        assertFalse("it must stop", StartupPolicy.shouldKeepTrying(StartupPolicy.MAX_ATTEMPTS))
        // Long enough for a real boot, short enough to be an honest message.
        assertTrue(
            "total wait was ${StartupPolicy.totalWaitSeconds()}s",
            StartupPolicy.totalWaitSeconds() in 120..600,
        )
    }

    // -- honest answers -----------------------------------------------------

    @Test
    fun `an unknown network name is reported as nothing, not as a name`() {
        assertNull(Capabilities.normalizeSsid("<unknown ssid>", wifiEnabled = true))
        assertNull(Capabilities.normalizeSsid("0x", wifiEnabled = true))
        assertNull(Capabilities.normalizeSsid("", wifiEnabled = true))
        assertNull(Capabilities.normalizeSsid(null, wifiEnabled = true))
        assertNull("wifi off means no network, whatever is cached",
                   Capabilities.normalizeSsid("HomeNet", wifiEnabled = false))
        assertEquals("HomeNet", Capabilities.normalizeSsid("\"HomeNet\"", wifiEnabled = true))
    }
}
