package com.vault.phone

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.dp

/**
 * Showing a document that is not a .txt.
 *
 * Most of what lands in a vault is a scan or a photograph, so a phone that
 * can only open plain text cannot really show the vault. Pictures are decoded
 * here; PDFs are drawn on the PC (form fields included) and arrive as pages.
 */

/** What the viewer managed to turn the bytes into. */
sealed interface Rendered {
    /** `total` is the document's page count; `warning` is what the PC said about it. */
    data class Pages(val pages: List<Bitmap>, val total: Int = pages.size, val warning: String = "") : Rendered
    data class Failed(val why: String) : Rendered
}

/** Width the PC renders a PDF page at: readable on a phone, cheap enough to hold. */
const val PDF_PAGE_WIDTH_PX = 1000
/** Pages held at once; each is ~2 MB in RGB_565. The PC serves at most 50. */
const val PDF_MAX_PAGES = 30
private const val MAX_IMAGE_PX = 2400

/** A page the PC drew: decoded at half the memory, as a scan needs no alpha. */
fun decodePage(png: ByteArray): Bitmap? = BitmapFactory.decodeByteArray(
    png, 0, png.size, BitmapFactory.Options().apply { inPreferredConfig = Bitmap.Config.RGB_565 })

fun renderImage(bytes: ByteArray): Rendered {
    // Measure first, then load downscaled: a 12-megapixel photograph decoded
    // at full size is a quick way to run a phone out of memory.
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
    var sample = 1
    while (maxOf(bounds.outWidth, bounds.outHeight) / sample > MAX_IMAGE_PX) sample *= 2
    val bitmap = BitmapFactory.decodeByteArray(
        bytes, 0, bytes.size, BitmapFactory.Options().apply { inSampleSize = sample }
    ) ?: return Rendered.Failed("this picture could not be decoded on the phone")
    return Rendered.Pages(listOf(bitmap))
}


@Composable
fun DocumentViewer(name: String, rendered: Rendered, modifier: Modifier = Modifier) {
    Column(modifier.fillMaxSize()) {
        Text(
            name,
            style = MaterialTheme.typography.titleSmall,
            modifier = Modifier.padding(horizontal = 20.dp, vertical = 10.dp),
        )
        when (rendered) {
            is Rendered.Failed -> Box(
                Modifier.fillMaxSize().padding(30.dp), contentAlignment = Alignment.TopCenter
            ) {
                Text(rendered.why, style = MaterialTheme.typography.bodySmall,
                     color = MaterialTheme.colorScheme.error)
            }

            is Rendered.Pages -> LazyColumn(
                Modifier.fillMaxSize().padding(horizontal = 10.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                if (rendered.warning.isNotBlank()) {
                    item {
                        Text(rendered.warning, style = MaterialTheme.typography.labelMedium,
                             color = MaterialTheme.colorScheme.error,
                             modifier = Modifier.padding(horizontal = 10.dp))
                    }
                }
                items(rendered.pages) { page ->
                    Image(
                        bitmap = page.asImageBitmap(),
                        contentDescription = name,
                        contentScale = ContentScale.FillWidth,
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
                if (rendered.pages.size < rendered.total) {
                    item {
                        Text(
                            "first ${rendered.pages.size} of ${rendered.total} pages — open it at the desk for the rest",
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(16.dp),
                        )
                    }
                }
            }
        }
    }
}
