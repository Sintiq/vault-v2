# Read-only PDF preview API

The server slice started from `cf5fa60`; the current Android client behavior is
described separately below. These routes use the existing API key authorization.
Prefer `Authorization: Bearer <key>`;
examples below omit the key. Missing or wrong authorization returns 401 before
opening a PDF. The existing key/query compatibility is unchanged.

## Requests

`GET /api/pdf/info?pane=documents&rel=example.pdf`

Returns HTTP 200 JSON: `{"pages": 12, "warning": ""}`. `pages` is the actual
document count, including when it exceeds 50. `warning` is exactly the desktop
form warning: empty when the structural check succeeds without XFA, or an honest
XFA/unchecked-structure warning. This reads metadata without rasterizing a page.

`GET /api/pdf/page?pane=documents&rel=example.pdf&page=0&width=1080`

Returns HTTP 200 `image/png`, through the same Qt-free `render_pdf_page` used on
the desktop. `pane`, `rel` and `page` are required. `width` is optional and
defaults to 1080; explicit empty values are invalid. Numeric values must be
integers; width is 1–1600 inclusive. Page indexes start at zero, must exist in the
document, and must be below 50. A 51-page document can show pages 0–49; page 50 or
higher returns HTTP 400 `{"error": "too many pages for the phone"}`. Other invalid
page/width values return 400 JSON with an `error` string.

`pane` is `staging`, `documents` or `personal`. `rel` is a nonempty relative file
path in that pane. Encode query values once; for example a literal `%20.pdf` file
name is encoded as `%2520.pdf`. These routes do not perform a second URL decode.
Absolute/drive-relative paths, traversal, hidden/service entries, links,
junctions, directories, missing files and non-PDF files are rejected with 400.
The lexical path is retained until `visible_file` checks it. This does not
sandbox a hostile process replacing filesystem entries after validation.

## Current Android client

The viewer fetches `info`, then requests at most the first **30 pages**, each at
**1000 pixels wide**. It shows the form warning and a notice when more pages
remain. These are client choices, not the server's 50-page limit or 1080-pixel
default.

Each page request retries `busy` at most four times: **five attempts total**,
including the initial request, with waits of 400, 800, 1200 and 1600 ms. The
initial `info` request is outside this retry loop. The server still returns busy
without queuing or retrying; all of these retries are made by the phone.

## Concurrency and read-only behavior

One process-wide admission gate schedules bounded PDF children for both endpoints,
the desktop and native text extraction. Native PDFium/pypdf objects never live
in the window process. A valid HTTP job tries admission once without waiting: if
busy, HTTP returns 503 `{"error": "busy"}` before reading another source file.
There is no queued request or automatic server retry. The caller can retry later.
The desktop waits in its background worker, with cancellation and the same
10-second operation deadline. Failure cleans up the child and releases admission
so a later request can proceed; separate app processes do not share this gate.
Metadata/render child failure, timeout or invalid output returns a sanitized
HTTP 400 error; `/api/ping` remains independently responsive during a PDF job.

All responses use `Cache-Control: no-store`. PNG responses have a content length
and do not add a download filename. No source modification, output file, receipt,
model call or Vault write guard is involved. Read-only mode and an unrelated busy
Vault writer do not prevent a PDF read. The API key grants the same owner-view
access as other authenticated file routes; this is not a public document service.

## Qualified scope

Synthetic HTTP tests cover filled AcroForms with and without `/AP`, including
AES owner-password restrictions with an empty opening password, pixel comparison
against a blank form, XFA/unchecked warnings, count/index/width limits, busy
desktop and HTTP jobs, error recovery, authorization and filesystem boundaries.
The existing renderer also enforces finite geometry, a maximum rendered height
and a pixel budget. PDF source input is at most 512 MiB, child output at most
64 MiB, and each metadata/page operation has a 10-second deadline. Bounded
source reads occur before sending bytes to the child; blocked filesystem I/O
and Windows Job cleanup can add wall time. No whole-document time or child
memory-allocation guarantee is claimed. Owner documents are not used in these checks. XFA data is not promised
to render; the phone must show the `info.warning` rather than hide it. This slice
does not establish phone UI or owner-document end-to-end verification.
