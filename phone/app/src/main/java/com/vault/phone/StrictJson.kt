package com.vault.phone

/**
 * The release manifest read token by token, as strictly as the PC reads it
 * (docs/release-format-v1.md): one flat object; values only strings and whole
 * numbers; no duplicate keys, no floats, booleans, nulls, arrays or nested
 * objects; nothing after the closing brace. Anything else is not a manifest.
 */
object StrictJson {
    class Invalid(why: String) : Exception(why)
    private const val HEX = "0123456789abcdefABCDEF"

    fun flatObject(text: String): Map<String, Any> {
        val p = Parser(text)
        p.ws()
        p.expect('{')
        val out = LinkedHashMap<String, Any>()
        p.ws()
        if (p.peek() == '}') {
            p.next()
        } else {
            while (true) {
                p.ws()
                val key = p.string()
                if (out.containsKey(key)) throw Invalid("duplicate key $key")
                p.ws()
                p.expect(':')
                p.ws()
                out[key] = when (val c = p.peek()) {
                    '"' -> p.string()
                    '-', in '0'..'9' -> p.integer()
                    else -> throw Invalid("unexpected value starting with '$c'")
                }
                p.ws()
                when (p.next()) {
                    ',' -> continue
                    '}' -> break
                    else -> throw Invalid("expected , or }")
                }
            }
        }
        p.ws()
        if (!p.atEnd()) throw Invalid("text after the object")
        return out
    }

    private class Parser(val s: String) {
        var i = 0
        fun atEnd() = i >= s.length
        fun peek(): Char = if (atEnd()) throw Invalid("unexpected end") else s[i]
        fun next(): Char = peek().also { i++ }
        fun ws() { while (!atEnd() && s[i] in " \t\r\n") i++ }
        fun expect(c: Char) { if (next() != c) throw Invalid("expected $c") }

        fun string(): String {
            expect('"')
            val b = StringBuilder()
            while (true) {
                val c = next()
                when {
                    c == '"' -> return b.toString()
                    c == '\\' -> when (val e = next()) {
                        '"', '\\', '/' -> b.append(e)
                        'b' -> b.append('\b'); 'f' -> b.append('\u000C'); 'n' -> b.append('\n')
                        'r' -> b.append('\r'); 't' -> b.append('\t')
                        'u' -> {
                            val hex = if (i + 4 <= s.length) s.substring(i, i + 4) else ""
                            if (hex.length != 4 || !hex.all { it in HEX }) throw Invalid("bad escape")
                            b.append(hex.toInt(16).toChar()); i += 4
                        }
                        else -> throw Invalid("bad escape")
                    }
                    c < ' ' -> throw Invalid("control character in a string")
                    else -> b.append(c)
                }
            }
        }

        fun integer(): Long {
            val start = i
            if (peek() == '-') i++
            if (atEnd() || s[i] !in '0'..'9') throw Invalid("bad number")
            if (s[i] == '0' && i + 1 < s.length && s[i + 1] in '0'..'9') throw Invalid("leading zero")
            while (!atEnd() && s[i] in '0'..'9') i++
            if (!atEnd() && s[i] in ".eE") throw Invalid("not a whole number")
            return s.substring(start, i).toLongOrNull() ?: throw Invalid("number out of range")
        }
    }
}
