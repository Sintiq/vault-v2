package com.vault.phone

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The phone must read the PC's answer to a confirmation, not infer it from the
 * status code or from what was ticked. Each test states the rule it defends.
 */
class AcceptTest {
    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun `a refused stale selection is a changed-proposals error, not a lost connection`() {
        val e = VaultClient.failure(409, "proposals changed — refresh the list")
        assertTrue(e is VaultClient.ProposalsChanged)
        assertEquals("proposals changed — refresh the list", e.message)
    }

    @Test
    fun `a busy vault and a refused key are told apart`() {
        assertTrue(VaultClient.failure(503, "vault is busy — try again") is VaultClient.VaultBusy)
        assertTrue(VaultClient.failure(401, null) is VaultClient.NotConnected)
        assertTrue("an ordinary refusal keeps the connection",
                   VaultClient.failure(400, "bad request") !is VaultClient.NotConnected)
    }

    @Test
    fun `a plain success reads as the count the PC returned`() {
        val r = json.decodeFromString<AcceptResult>("""{"confirmed":2,"errors":[]}""")
        assertEquals("confirmed 2 card(s)", r.summary("confirmed", "card(s)"))
        val a = json.decodeFromString<AcceptResult>("""{"added":1}""")
        assertEquals("added 1 task(s)", a.summary("added", "task(s)"))
    }

    @Test
    fun `a partial failure names the three groups and asks for a refresh`() {
        val r = json.decodeFromString<AcceptResult>(
            """{"confirmed":1,"errors":["store write failed — effect unknown; refresh the list"],
               "outcome":{"completed":["a"],"failed_unknown":["b"],"not_attempted":["c","d"]}}""",
        )
        val s = r.summary("confirmed", "card(s)")
        assertTrue(s, s.contains("written 1") && s.contains("uncertain 1") && s.contains("not attempted 2"))
        assertTrue("must not be called done", s.contains("refresh") && !s.startsWith("confirmed"))
    }

    @Test
    fun `errors without an outcome are shown as they are`() {
        val r = json.decodeFromString<AcceptResult>("""{"confirmed":0,"errors":["x.pdf: shelf must be one of …"]}""")
        assertEquals("x.pdf: shelf must be one of …", r.summary("confirmed", "card(s)"))
    }

    @Test
    fun `the agent's jobs are given longer than the slowest measured health read`() {
        // 126 s was measured for one 6000-character health read on the owner's PC.
        assertTrue(VaultClient.AGENT_READ_TIMEOUT_S >= 250)
    }

    @Test
    fun `an Ask match says which pane and folder it lies in`() {
        val m = Json { ignoreUnknownKeys = true }.decodeFromString<AskMatch>(
            """{"name":"i-130.pdf","pane":"personal","rel":"IMMIGRATION/2026/i-130.pdf"}""")
        assertEquals("Personal / IMMIGRATION / 2026", m.where)
        val old = Json { ignoreUnknownKeys = true }.decodeFromString<AskMatch>("""{"name":"a.txt"}""")
        assertEquals("an old PC without pane/rel reads as Staging", "Staging", old.where)
    }
}
