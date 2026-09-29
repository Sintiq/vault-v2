package com.vault.phone

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.pdf.PdfRenderer
import android.os.Build
import android.os.ParcelFileDescriptor
import androidx.core.content.FileProvider
import java.io.File

/** A copy prepared on the PC's grant, ready to leave Vault the way the owner chose. */
data class PreparedExport(val grantId: String, val file: File, val name: String, val action: String)

/**
 * Hears which app the owner picked in Android's share sheet. Picking an app is all it
 * means: the receipt says «handed to <app>», never that a mail was sent.
 */
class ChooserReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val chosen: ComponentName? = if (Build.VERSION.SDK_INT >= 33)
            intent.getParcelableExtra(Intent.EXTRA_CHOSEN_COMPONENT, ComponentName::class.java)
        else @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_CHOSEN_COMPONENT)
        val op = intent.getStringExtra(EXTRA_OP) ?: return
        chosen?.packageName?.let { ExportOutcome.put(op, it) }
    }

    companion object {
        const val EXTRA_OP = "com.vault.phone.EXPORT_OP"
    }
}

/** Which app the owner picked, per export operation; read once. */
object ExportOutcome {
    private val chosen = java.util.concurrent.ConcurrentHashMap<String, String>()
    fun put(op: String, pkg: String) { chosen[op] = pkg }
    fun take(op: String): String? = chosen.remove(op)
}

object ShareSheet {
    fun intent(context: Context, prepared: PreparedExport): Intent {
        val uri = FileProvider.getUriForFile(context, context.packageName + ".export", prepared.file)
        val send = Intent(Intent.ACTION_SEND).apply {
            type = context.contentResolver.getType(uri) ?: "application/octet-stream"
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        val callback = PendingIntent.getBroadcast(
            context, prepared.grantId.hashCode(),
            Intent(context, ChooserReceiver::class.java).putExtra(ChooserReceiver.EXTRA_OP, prepared.grantId),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE,
        )
        return Intent.createChooser(send, "Send «${prepared.name}» with…", callback.intentSender)
    }
}

/** A PDF kept on this phone, drawn by Android. Filled form fields may not show here. */
fun renderLocalPdf(file: File): Rendered = runCatching {
    ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY).use { fd ->
        PdfRenderer(fd).use { pdf ->
            val pages = (0 until minOf(pdf.pageCount, PDF_MAX_PAGES)).map { i ->
                pdf.openPage(i).use { page ->
                    val w = PDF_PAGE_WIDTH_PX
                    val h = (w.toFloat() * page.height / page.width).toInt()
                    Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888).also {
                        it.eraseColor(Color.WHITE)
                        page.render(it, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                    }
                }
            }
            Rendered.Pages(pages, pdf.pageCount,
                           "drawn on this phone without the PC — filled form fields may not show")
        }
    }
}.getOrElse { Rendered.Failed("this PDF could not be drawn on the phone") }
