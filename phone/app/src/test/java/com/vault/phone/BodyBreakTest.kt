package com.vault.phone

import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * An answer that begins and then breaks off is «not reached» in every kind of read.
 * From Codex's review of 3a07290: short synthetic HTTP answers on loopback, no real vault.
 */
class BodyBreakTest {
    private fun refusesBrokenBody(read: (VaultClient) -> Unit) {
        val socket = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        val worker = Executors.newSingleThreadExecutor()
        try {
            socket.soTimeout = 5000
            val answer = worker.submit {
                socket.accept().use { client ->
                    client.soTimeout = 5000
                    val input = client.getInputStream().bufferedReader()
                    while (!input.readLine().isNullOrEmpty()) { }
                    client.getOutputStream().write(
                        "HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nXX".toByteArray())
                    client.getOutputStream().flush()
                }
            }
            val client = VaultClient("http://127.0.0.1:${socket.localPort}", "synthetic-only")
            val error = runCatching { read(client) }.exceptionOrNull()
            answer.get(6, TimeUnit.SECONDS)
            assertTrue("must be handled NotConnected, got $error", error is VaultClient.NotConnected)
        } finally {
            socket.close(); worker.shutdownNow(); worker.awaitTermination(3, TimeUnit.SECONDS)
        }
    }

    @Test fun `manifest body break is handled`() = refusesBrokenBody { it.releaseManifest(); Unit }
    @Test fun `ordinary JSON body break is handled`() = refusesBrokenBody { it.tasks(); Unit }
    @Test fun `PDF byte body break is handled`() = refusesBrokenBody { it.pdfPage("staging", "synthetic.pdf", 0, 100); Unit }
}
