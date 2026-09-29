package com.vault.phone

import android.net.Uri
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/**
 * What the PC's QR carries: the vault's address and a one-time ticket, never a key.
 * `vault-pair://pair?u=<address>&t=<ticket>`. Pure, so it is tested without a phone.
 */
data class PairLink(val base: String, val ticket: String) {
    companion object {
        private val TICKET = Regex("[A-Za-z0-9_-]{16,64}")

        fun parse(text: String): PairLink? {
            val trimmed = text.trim()
            if (!trimmed.startsWith("vault-pair://")) return null
            return fromParts(queryParam(trimmed, "u"), queryParam(trimmed, "t"))
        }

        fun fromUri(uri: Uri?): PairLink? {
            if (uri == null || uri.scheme != "vault-pair") return null
            return fromParts(uri.getQueryParameter("u"), uri.getQueryParameter("t"))
        }

        private fun fromParts(u: String?, t: String?): PairLink? {
            val base = u?.trim()?.trimEnd('/') ?: return null
            val ticket = t?.trim() ?: return null
            if (!(base.startsWith("https://") || base.startsWith("http://"))) return null
            if (!VaultAddress.plain(base)) return null
            if (!TICKET.matches(ticket)) return null
            return PairLink(base, ticket)
        }

        private fun queryParam(link: String, name: String): String? {
            val query = link.substringAfter('?', "")
            return query.split('&').firstNotNullOfOrNull { part ->
                val (k, v) = part.split('=', limit = 2).let { it[0] to it.getOrElse(1) { "" } }
                if (k == name) java.net.URLDecoder.decode(v, "UTF-8") else null
            }
        }
    }
}

/** The dot in the app bar. Green is an observation of an authorised answer, never a permission. */
enum class Link { GREEN, RED, AMBER }

object LinkState {
    const val FRESH_MS = 30_000L
    const val API_VERSION = 2

    /** [lastOkAt] and [now] are elapsedRealtime milliseconds; [apiVersion] is what the PC said. */
    fun of(lastOkAt: Long?, now: Long, apiVersion: Int?, vaultIdMatches: Boolean): Link = when {
        apiVersion != null && apiVersion != API_VERSION -> Link.AMBER
        lastOkAt == null || !vaultIdMatches -> Link.RED
        now - lastOkAt > FRESH_MS -> Link.RED
        else -> Link.GREEN
    }

    fun words(link: Link): String = when (link) {
        Link.GREEN -> "PC online"
        Link.RED -> "PC offline"
        Link.AMBER -> "Update needed"
    }
}

/** A vault address is a scheme, a host and a port: never user-info, a query or a fragment. */
object VaultAddress {
    fun plain(base: String): Boolean {
        val u = base.toHttpUrlOrNull() ?: return false
        return u.username.isEmpty() && u.password.isEmpty() && u.query == null && u.fragment == null
    }
}
