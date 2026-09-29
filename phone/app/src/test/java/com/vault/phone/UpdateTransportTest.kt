package com.vault.phone

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.io.File
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.nio.file.Files
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The update transport on tiny loopback servers: no device, installer, key or vault.
 * The first four cases are Codex's review reproductions (17b641c), moved to the
 * attempt-folder interface; the rest pin the job and the hand-over check.
 */
class UpdateTransportTest {
    private class TinyServer(val handler: (String, OutputStream) -> Unit) : Closeable {
        private val socket = ServerSocket(0, 8, InetAddress.getByName("127.0.0.1"))
        private val jobs = Executors.newCachedThreadPool()
        val port get() = socket.localPort
        init {
            jobs.submit {
                while (!socket.isClosed) {
                    val client = runCatching { socket.accept() }.getOrNull() ?: break
                    jobs.submit { client.use { s ->
                        s.soTimeout = 5000
                        val input = s.getInputStream().bufferedReader()
                        val path = input.readLine().split(' ')[1]
                        while (!input.readLine().isNullOrEmpty()) { }
                        handler(path, s.getOutputStream())
                    } }
                }
            }
        }
        override fun close() { socket.close(); jobs.shutdownNow(); jobs.awaitTermination(3, TimeUnit.SECONDS) }
    }

    private fun respond(out: OutputStream, body: String, declared: Int = body.length) {
        out.write("HTTP/1.1 200 OK\r\nContent-Length: $declared\r\nConnection: close\r\n\r\n$body".toByteArray())
        out.flush()
    }

    private fun manifest(bytes: ByteArray) = ReleaseManifest("vault-v2-release@1", "0.5", 5,
        "android", "vault.apk", bytes.size.toLong(),
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }, 2)

    private fun parts(dir: File) = dir.walk().filter { it.name.endsWith(".part") }.toList()

    @Test fun `one valid download keeps exactly its verified bytes`() {
        val server = TinyServer { _, out -> respond(out, "GOOD") }
        val dir = Files.createTempDirectory("vault-valid-body-").toFile()
        try {
            val bytes = "GOOD".toByteArray()
            val apk = VaultClient("http://127.0.0.1:${server.port}", "").downloadRelease(manifest(bytes), dir)
            assertArrayEquals(bytes, apk.readBytes())
            assertEquals("the file lives in a folder of its own", dir, apk.parentFile.parentFile)
            assertTrue(parts(dir).isEmpty())
        } finally { server.close(); dir.deleteRecursively() }
    }

    @Test fun `body disconnect is a handled vault refusal and leaves nothing`() {
        val server = TinyServer { _, out -> respond(out, "XX", 10) }
        val dir = Files.createTempDirectory("vault-short-body-").toFile()
        try {
            val client = VaultClient("http://127.0.0.1:${server.port}", "")
            val error = runCatching { client.downloadRelease(manifest(ByteArray(10)), dir) }.exceptionOrNull()
            assertTrue("body IO escaped the handled boundary: $error", error is VaultClient.NotConnected)
            assertTrue("the attempt folder must be removed", dir.listFiles()!!.isEmpty())
        } finally { server.close(); dir.deleteRecursively() }
    }

    @Test fun `userinfo cannot impersonate the loopback origin exemption`() {
        var hits = 0
        val server = TinyServer { _, out -> hits++; respond(out, "{}") }
        try {
            // The authority before @ is userinfo, NOT the HTTP host. All traffic stays local.
            val client = VaultClient("http://127.0.0.1:80@localhost:${server.port}", "")
            val error = runCatching { client.releaseManifest() }.exceptionOrNull()
            assertTrue("userinfo prefix was accepted; local request count=$hits, error=$error",
                error is VaultClient.VaultException && hits == 0)
        } finally { server.close() }
    }

    @Test fun `a second download cannot touch the bytes of one in progress`() {
        val workers = Executors.newCachedThreadPool()
        val finishFirst = CountDownLatch(1)
        val body = "GOOD".toByteArray()
        val server = TinyServer { path, out ->
            if (path.startsWith("/first/")) {
                out.write("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n4\r\nGOOD\r\n".toByteArray())
                out.flush()
                check(finishFirst.await(8, TimeUnit.SECONDS))
                out.write("0\r\n\r\n".toByteArray()); out.flush()
            } else respond(out, "XX") // over its manifest limit
        }
        val dir = Files.createTempDirectory("vault-overlap-").toFile()
        try {
            val first = VaultClient("http://127.0.0.1:${server.port}/first", "")
            val second = VaultClient("http://127.0.0.1:${server.port}/second", "")
            val future = workers.submit<Result<File>> { runCatching { first.downloadRelease(manifest(body), dir) } }
            val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
            while (parts(dir).firstOrNull()?.length() != body.size.toLong() && System.nanoTime() < deadline) Thread.sleep(10)
            assertEquals("fixture: first body has reached its part", body.size.toLong(), parts(dir).firstOrNull()?.length())
            val secondError = runCatching { second.downloadRelease(manifest("X".toByteArray()), dir) }.exceptionOrNull()
            assertTrue("oversize second attempt must be refused", secondError is VaultClient.VaultException)
            finishFirst.countDown()
            val first1 = future.get(8, TimeUnit.SECONDS)
            assertNull("second attempt broke first verified download: ${first1.exceptionOrNull()}", first1.exceptionOrNull())
            assertArrayEquals("first download returned bytes that were not hashed", body, first1.getOrThrow().readBytes())
        } finally {
            finishFirst.countDown(); server.close(); workers.shutdownNow(); workers.awaitTermination(3, TimeUnit.SECONDS)
            dir.deleteRecursively()
        }
    }

    @Test fun `only one update job runs at a time, and a failed one frees the way`() {
        val inside = CountDownLatch(1)
        val release = CountDownLatch(1)
        val workers = Executors.newSingleThreadExecutor()
        try {
            val first = workers.submit<String> { UpdateJob.run { inside.countDown(); release.await(5, TimeUnit.SECONDS); "first" } }
            assertTrue(inside.await(5, TimeUnit.SECONDS))
            val second = runCatching { UpdateJob.run { "second" } }.exceptionOrNull()
            assertTrue("a second update while one runs is refused: $second", second is VaultClient.VaultException)
            release.countDown()
            assertEquals("first", first.get(5, TimeUnit.SECONDS))
            runCatching { UpdateJob.run { error("broke") } }
            assertEquals("after a failure the next update may start", "next", UpdateJob.run { "next" })
        } finally { release.countDown(); workers.shutdownNow() }
    }

    @Test fun `the bytes handed to the installer are checked as they are handed over`() {
        val good = "GOOD".toByteArray()
        val m = manifest(good)
        val out = ByteArrayOutputStream()
        ReleaseRules.copyVerified(ByteArrayInputStream(good), out, m)
        assertArrayEquals(good, out.toByteArray())
        for (changed in listOf("GOOF", "GOODX", "GOO")) {
            val error = runCatching {
                ReleaseRules.copyVerified(ByteArrayInputStream(changed.toByteArray()), ByteArrayOutputStream(), m)
            }.exceptionOrNull()
            assertTrue("$changed must not pass as the signed release", error is VaultClient.VaultException)
        }
    }

    @Test fun `a vault address never carries user-info`() {
        assertFalse(VaultAddress.plain("http://127.0.0.1:80@localhost:8777"))
        assertFalse(VaultAddress.plain("https://someone:pw@vault.example.ts.net"))
        assertTrue(VaultAddress.plain("https://vault.example.ts.net"))
        assertNull(PairLink.parse("vault-pair://pair?u=http%3A%2F%2F127.0.0.1%3A80%40evil%3A8777%2F&t=" + "a".repeat(20)))
        assertEquals("https://vault.example.ts.net",
            PairLink.parse("vault-pair://pair?u=https%3A%2F%2Fvault.example.ts.net%2F&t=" + "a".repeat(20))?.base)
    }
}
