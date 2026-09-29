package com.vault.phone

import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Secrets at rest: the device token and the door key, sealed with an AES-GCM key
 * that lives in the Android Keystore and never leaves it. The PIN is not this key;
 * six digits would be too few to derive one. The wipe deletes the Keystore key,
 * which makes every sealed value unreadable even if a file survived.
 */
object SecretBox {
    private const val ALIAS = "vault-secrets"
    private const val PREFIX = "sealed:"

    private fun keyStore(): KeyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun key(): SecretKey {
        (keyStore().getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return gen.generateKey()
    }

    fun seal(plain: String): String {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val body = cipher.iv + cipher.doFinal(plain.toByteArray(Charsets.UTF_8))
        return PREFIX + Base64.getEncoder().encodeToString(body)
    }

    /** Null when the value cannot be opened (key deleted by a wipe, or damaged). */
    fun open(sealed: String): String? = runCatching {
        val body = Base64.getDecoder().decode(sealed.removePrefix(PREFIX))
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, body, 0, 12))
        String(cipher.doFinal(body, 12, body.size - 12), Charsets.UTF_8)
    }.getOrNull()

    fun isSealed(value: String?): Boolean = value?.startsWith(PREFIX) == true

    /** Read a secret; a plaintext one left by an older build is sealed on the way out. */
    fun read(prefs: SharedPreferences, name: String): String? {
        val raw = prefs.getString(name, null) ?: return null
        if (isSealed(raw)) return open(raw)
        prefs.edit().putString(name, seal(raw)).apply()
        return raw
    }

    fun write(editor: SharedPreferences.Editor, name: String, plain: String): SharedPreferences.Editor =
        editor.putString(name, seal(plain))

    /** True when the key is gone (or never existed). */
    fun deleteKey(): Boolean = runCatching {
        val ks = keyStore()
        if (ks.containsAlias(ALIAS)) ks.deleteEntry(ALIAS)
        !keyStore().containsAlias(ALIAS)
    }.getOrDefault(false)
}
