# FrameAtlas

A side project for finding the footage you already have, built alongside
[AutoCut](https://github.com/moheshmohan/AutoCut). The browser UI and
stdio MCP server share a local SQLite catalogue. Google Photos and Insta360 remain the sources
of record; the app retains metadata, timecoded scene descriptions, transcripts, tags, categories,
editor corrections, story ideas, and small previews. It does not maintain another library of full originals.

<p align="center">
  <a href="https://www.youtube.com/watch?v=P3VMqTmsAlM"><img src="https://img.youtube.com/vi/P3VMqTmsAlM/maxresdefault.jpg" width="720" alt="Video: An AI Agent Edited This Video on My MacBook | AutoCut + FrameAtlas (Malayalam, with English subtitles)"></a>
</p>

**[Watch: an AI agent edited this video on my MacBook](https://www.youtube.com/watch?v=P3VMqTmsAlM)** (Malayalam, English subtitles): FrameAtlas found
the footage for it. **[Blog post: how to run FrameAtlas and AutoCut, by hand and with an agent](https://www.h4hacks.com/2026/11/blog-post_21.html)**.

## Run on this Mac

Double-click **Open FrameAtlas.command**, or:

```sh
git clone https://github.com/moheshmohan/FrameAtlas
cd FrameAtlas
./run.sh serve
```

Open **http://127.0.0.1:7318**. Keep the process running while using the UI or processing MCP jobs.
Ctrl-C stops it safely. Python 3.9+ and the already installed FilmCraft CLI are required. The UI,
SQLite FTS search, importers, queue and MCP server have **no pip or npm dependencies**. No external
fonts, analytics, CDNs, hosted database or hosted AI services are used.

Data live outside this repository at `~/Library/Application Support/FrameAtlas/`.
Set `FRAMEATLAS_DATA` to use another private data directory. Do not put it inside a synced/public folder.

For long indexing passes, `./run.sh worker` runs a separate local queue worker. A subsequently
opened UI attaches to it; closing that UI does not stop the background pass. Exactly one worker
owns the catalogue lock. For a planned restart, a worker that announces drain support accepts
`kill -USR1 PID` on macOS and Linux. It finishes its current group and any Picker work already
in lookahead, subject to the existing pause and priority checks, then exits without changing
the saved queue setting. Wait for the process to exit before starting its replacement.
Ctrl-C/SIGTERM stop processing and retain queued work. Restart `serve` after stopping a
separately attached worker if you want the UI to own processing again.

## Back up and restore the catalogue

Create a portable ZIP while the local app is running:

```sh
./run.sh backup /absolute/path/FrameAtlas-backup.zip
./run.sh inspect-backup /absolute/path/FrameAtlas-backup.zip
```

The default backup includes a consistent SQLite snapshot and referenced small previews,
SHA-256 payload checksums, row counts, restore instructions and a standalone Python restore
tool. It excludes full originals, model weights, credentials and temporary downloads.
Use `--database-only` to omit previews. Upload the completed ZIP to private off-device
storage such as your own Google Drive. The backup command does not upload it automatically.

On a new instance, install this code checkout and Python 3.9+, then:

```sh
./run.sh restore /absolute/path/FrameAtlas-backup.zip --to /absolute/path/FrameAtlas-restored
FRAMEATLAS_DATA=/absolute/path/FrameAtlas-restored ./run.sh serve --port 7319
```

The destination must **not exist**. Restore validates every payload checksum, SQLite integrity,
foreign keys and saved row counts before publishing. No existing catalogue is replaced. The queue
starts paused, interrupted jobs need explicit retry, and old editing retrieval requests are cancelled.
Machine-specific local source paths are cleared by default; use `--keep-local-paths` only for
intentional same-computer recovery. Historical provenance and stable cloud IDs remain.
Reconnect Photos/Studio, relink local originals and install native models separately when needed.
Expired Google selections need refreshing only when downloading originals, not for metadata search.

The ZIP also contains `RESTORE.txt` and `restore_frameatlas.py`. If no FrameAtlas checkout is
available yet, extract only that Python file and run `python3 restore_frameatlas.py restore
/absolute/path/backup.zip --to /absolute/path/NEW-data-folder`. A code checkout is still required
to run the restored web app. Checksums detect accidental corruption, not malicious replacement.

## Catalogue features

- Metadata import from existing AutoCut Google Photos selections and Studio local recordings.
  Saved Photos sidecars are also recovered when their cached originals were previously removed;
  no media are downloaded to recover these records.
- Reviewed Studio Cloud list screenshot import, with local CPU text recognition or a local vision
  model, split recording names, distinct MP4/INSV files, timestamp-offset warnings, evidence hashes,
  atomic batch import and explicit unverified OCR provenance. A later Studio download attaches to
  the same catalogue file instead of creating a second record.
- Persistent selection-batch receipts shared by the UI and MCP: expiry, import counts, current
  source links and pending-job counts, with guarded closing. Receipts survive app restarts without
  storing Picker handoff URLs or media download URLs. Newer selections refresh the same stable IDs.
- Fresh Google Photos Picker sessions through a small Rust broker that reuses FilmCraft's secure
  OAuth vault. Credentials and expiring bearer URLs are never returned to the browser or stored
  in the catalogue. Selection import reads metadata; fetching happens only when analysing.
- Metadata-only Google Takeout import: read JSON sidecars directly from `.zip`, `.tar.gz`, `.tgz`,
  `.tar`, an individual JSON file, or a folder containing up to 32 archive files. **No original
  media are extracted.** Each archive imports atomically, and a folder import reports the result
  for every archive.
- Explicit JSON cloud manifests, stable filenames/IDs, cloud links when available, source retrieval
  plans, and read-only association of an existing original.
- Priority editing retrieval: reuse an existing full original immediately, or download a selected Google
  source into an explicitly chosen editing folder. Completed jobs return an editor-ready path and
  paired 360 camera/proxy references. This does not displace its pending analysis job.
- Native local scene detection and descriptions with TransNetV2 and Qwen3-VL 2B (fast) / 4B
  (detailed multi-keyframe mode). Native local Whisper speech recognition with Silero speech detection.
  Language/model options are available in the analysis dialog and MCP.
- Calibrated 360 analysis: native original import retains lens/gyro metadata and automatically
  attaches the camera proxy. Original shot descriptions are augmented by six directions at sampled
  moments. Search returns the matching direction and sample time; the UI groups those observations.
- Persistent, single-worker queue: bounded requests, duplicate active-job prevention, cancellation,
  retry, interrupted-job recovery, per-stage status and actionable errors.
  Selected analysis can run ahead of the background backlog; editing retrieval retains precedence.
- FTS5 keyword search across names, scene descriptions, spoken words, tags and categories; source,
  date, media type, analysis state and category filters. Exact filenames rank first, followed by
  keyword relevance. Choose **Exact filename** in the browser or set `filename` in
  `catalog_search` to match the full filename, including the extension. Use source and date
  filters to distinguish multiple records with the same name. A scene timestamp requires all
  search terms in the scene, and a speech
  timestamp covers a contiguous spoken phrase. Metadata-only matches can appear without timestamps.
  The browser opens matching scenes and 360 views directly and highlights matching transcript words.
  Speech hits include zero-based inclusive word indices for MCP clients.
  Visual search hits include a scene key. For a directional hit, pass its
  `sample_time` and `scene_key` to `catalog_asset` to resolve the indexed
  view. For an ordinary scene, pass the start, end and scene key. Include
  the exact part filename for a multipart recording. The web app opens
  the matching scene and displays editing timecodes to milliseconds.
- Automatic explainable categories and model-derived tags. Editable summaries, tags and scene
  captions. Corrections survive reimports/reanalysis of the same scene range. Original model output
  remains separately in provenance and does not replace editor corrections in scene search.
  If a scene moves during reanalysis, reopen the item before saving its caption. MCP clients can
  send the `scene_key` from `catalog_asset` alongside the ordinal to get the same stale-edit check.
- Local Ollama story brainstorming (the **From footage** way into **Ideas**) with source IDs and
  validated scene-time citations. Every story lands on the Idea Pad with its edit sequence.
  Generated prose remains a draft: valid citations do not guarantee every claim is correct.
  New drafts use the validated citations as ordered edit steps and list optional pickup shots
  separately. MCP still returns an `outline` for text clients, built from the same steps.
  Select analysed files in the library and choose **Story from selection** to restrict
  the draft to those exact files across providers. MCP clients can pass the same
  catalogue IDs in `catalog_ideas.asset_ids` for up to 18 files.
  **Story from results** uses the current literal Library query and filters, including source
  access, to find up to 18 matching files with scene captions. Matching scenes and
  timed speech guide which shots are supplied to the local model. MCP callers can
  pass the Library filters as `catalog_ideas.search_scope`, separately from the story prompt.
  **Look for related wording locally** uses the installed writing model to suggest short phrases
  and searches those phrases against the local catalogue. MCP callers can set
  `catalog_ideas`'s `expand_search` option. The draft lists the phrases it searched.
  Click a draft citation to open its indexed scene or exact 360 sample. MCP clients can pass
  citation times and the saved scene key to `catalog_asset` to check that the scene still exists.
- In the library, **Search related wording locally** uses the installed Phi-4 Mini model to suggest
  alternate phrases. Each result shows its matched wording and uses the same indexed edit references
  as keyword search. The current filters apply to all phrases. MCP clients can use
  `catalog_search_related` with Phi-4 Mini or Qwen3 8B. Related search returns up to 24 items.
- Responsive browser interface, JSON metadata export, source provenance, import coverage and storage use.
- Stdio MCP tools use the same implementation as the UI. See `.mcp.json`.

### Ideas (the Idea Pad)

**Ideas** (in the sidebar) is one pad for every video idea, whichever way it started:

- **✎ Write one**: title, one line, notes, format and tags.
- **✧ From moments**: a local writing model proposes ideas from *moments* the metadata shows:
  days or runs of days with far more footage than usual, days with 360 recordings or several
  videos, and days whose anniversary comes up in the next three weeks. Each moment carries its
  counts, devices, places, named people, tags and a few analysed items' descriptions. The model
  picks a moment, writes a title, a logline and why it is worth making now, and chooses shots from
  that moment only; **Footage from these days →** opens them in the Library. Moments that already
  have an idea are skipped next time unless you include them. Set a date range or a focus (for
  example `360` or `beach`) to steer it.
- **▷ From footage**: describe a story, or choose **Story from selection** / **Story from results**
  in the Library. The model builds an edit sequence whose every shot is an indexed file and time
  range (see the story bullet above); each of its takes is saved as an idea.

Every idea, whatever its origin, can **Build edit sequence** (or **Rebuild**): the model picks real
shots for it from its linked analysed items, else the days of its moment, else a catalogue search
for its title and line. The sequence (cited shots in editing order, pickup shots still needed,
other takes) opens from the card; each shot opens the item at that time. Ideas move through idea →
planned → shooting → editing → published (or dropped); title, format and notes are edited in
place; filter by status or by where they came from; **Copy** puts an idea and its shot list on the
clipboard. Writing models: Gemma 3 12B (best writing), Spark-X2.6 4B (fast), Qwen3 8B or Phi-4 Mini
through the loopback Ollama service; with none chosen, the best one installed. Story explorations
saved before the two pages became one are moved onto the pad the first time it opens.

### People

**People** (in the sidebar) finds the faces in your media and groups them into people you can
name. Everything runs on this Mac through FilmCraft: YuNet finds each face, its five landmarks
align it, and **AuraFace-v1** (Apache-2.0, an ArcFace-trained ResNet-100, on the GPU) turns it into
a 512-number fingerprint; `faces.group` groups the fingerprints (nearest neighbours, then Chinese
Whispers). Press **Find faces in my library** to scan the stored previews (about 40 a second on an
M-series Mac); items analysed from their originals get full-resolution faces (the photo, or six
frames spread over a video), which replace the preview's.

- Name a person on their card; names survive regrouping. Select two cards to merge them.
- Open a person to correct them: select faces and **Link to another person…** (or a new person) or
  **Unlink** them; a face moved or unlinked never goes back to that person. **Merge with…** joins
  two people who are the same person. The picker suggests the people the faces look most like.
- **Unsorted faces** lists the faces that belong to no one yet (passers-by, faces the grouping
  was unsure of): select some and link them to a person. In an item's details, a face marked
  **Who is this?** links the same way.
- **To review** is the sorter's desk. **Sort automatically** merges people whose faces are clearly
  one person (fingerprint likeness 0.8 or more; two different names are never merged on their own)
  and links unsorted faces that clearly belong to someone; less certain merges, links, moves and
  unlinks wait there for a yes or no. **Find suggestions** only proposes. Faces you confirmed are
  never touched, a "no" is not asked again, and every change can be undone.
- Names from **Google Photos' People & pets** can be brought over: each named person's photos are
  matched to the catalogue by their local capture time (to the second, the time zone found by
  trying every quarter-hour offset) and kind, and the person seen in most of them gets the name.
  Names you gave are never replaced (a different Google name is only proposed), and groups that
  turn out to be the same Google person are merged when their faces agree.

Agents sort faces the same way: `people_autosort` (propose, or `apply`), `people_review`,
`people_review_decide` (apply, dismiss, undo), and `people_compare`, which returns the face crops
as images so a model that can see can judge a merge or a link by eye. `people_import_names` takes
another library's named people as `{name, items: [{kind, local}]}`.
- **Hide** a passer-by without deleting anything. Groups of fewer than three faces get no person.
- **Media →** opens the Library filtered to that person; MCP's `catalog_search` takes `person`
  (one ID, or several for items with all of them). Named people appear in the Idea Pad's moments
  and in the ideas written about them.
- Face data stays in the catalogue (`faces/` holds 128 px crops); **Delete all face data** on the
  People page (or the `people_forget` action) removes every face, person and correction.

MCP tools: `people_status`, `people_scan`, `people_regroup`, `people_list`, `person_get`,
`person_update`, `person_merge`, `face_assign`, `faces_link`, `faces_unlink`, `faces_unsorted`,
`people_suggest`, `people_autosort`, `people_review`, `people_review_decide`, `people_compare`,
`people_import_names`. From a terminal: `python3 -m frameatlas.people scan`, `group` or `sort [--apply]`.

### Capture metadata: when, where, with what

An item's detail shows its weekday and time, camera, place and picture size, and **What the
library knows** on the Idea Pad shows how much of the catalogue has each:

- **Date and time** come from Google's capture time, camera filenames, Takeout or the file. Times
  with a zone are shown in this Mac's zone; times without one are the camera's clock.
- **Camera** (make and model) is recorded from Google's Picker for items picked from now on;
  earlier picks get it when they are selected again. Insta360 inventories record the camera model.
- **Device guesses** come from filename patterns (`IMG_1234.HEIC` → iPhone, `IMG-…-WA…` →
  WhatsApp, `DSC_…` → Nikon, `.insv` → Insta360 and so on) and are always labelled as guesses.
- **Place**: Google removes positions from Picker downloads and the Picker does not report them,
  so Google items get a place only from a linked Google Takeout record. Local video originals are
  read for embedded GPS (`python3 -m frameatlas.cli metadata-backfill` checks any never read).

## Offline boundary

**All media inference runs on this Mac.** FilmCraft does the decode, cuts, descriptions and speech
recognition. Ollama is restricted in code to `127.0.0.1:11434` for idea writing and optional screenshot OCR. There is no hosted-AI
fallback. Inference checks that weights are installed, sets offline model flags, and never calls a
model-download command. Missing weights produce an error. Native inference and local writing share
FilmCraft's GPU lock.

FrameAtlas excludes model aliases that Ollama advertises as remote before submitting prompts or
screenshots. Ollama also supports disabling cloud features for its entire service with
`OLLAMA_NO_CLOUD=1`. See [Ollama's local-only configuration](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features).

Cloud inventory selection/authentication/download still requires the Internet. Once a source and
models are available locally, scene descriptions and transcription require no cloud inference.
Files on disk are opened read-only without copying. Cloud selections are streamed to job-owned
temporary directories. Owned files are removed after success, failure or cancellation; a busy or unconfirmed
Studio volume is retained for safe retry rather than force-unmounted.
Cache quota: **2 GiB by default**, selectable up to **32 GiB**, plus a **3 GiB free-disk reserve**. Analysis acquisition reserves **256 MiB**
inside that quota for temporary perspective frames and working metadata. Editing retrieval can use
the full available cache allowance. Oversize sources fail with a clear
message instead of filling the disk. Restart cleanup only touches marked job directories owned by
this app, while its worker singleton lock is held. Existing FilmCraft and Studio downloads, exports
and project references are **never deleted**.

Small local JPEG previews are retained. New flat-video previews use a sampled frame from a
captioned scene when available, which helps when a clip opens on black. Scene cache and transcript
metadata may also be retained by the native FilmCraft engine. The catalogue directory stores no
full-resolution originals. User-selected editing downloads are retained only in explicitly chosen
editing folders.

## Honest cloud coverage and current limits

This is a working first version, **not a completed scan of both cloud libraries**.

Open **View coverage** in the Library to see the local inventory by source and capture month.
The report separates ready analysis, partial analysis, metadata-only items, and records with
transcript text. Filter by videos, photos or audio to see that media type in the source totals
and monthly rows. The report uses the stored capture calendar, including camera dates without
a verified time zone. Unlisted months can still contain cloud media. MCP exposes the same
report through `catalog_coverage` with optional `kind` and monthly source filters and pagination.
Click a monthly count to browse those exact catalogue records in the Library. MCP callers can
apply the same filters with `catalog_search` or `catalog_search_related`. Use `capture_month`
as `YYYY-MM` or `unknown`, plus the source and optional status `ready`, `partial` or
`metadata_only`.

Choose **Spoken words only** in the Library, or pass `speech_only: true` to search for a
contiguous phrase in transcript text. When word timings exist, a result opens the matching
words in the Transcript tab. Leave the query empty to browse items with transcript text,
including from the coverage table's transcript counts.

Choose **Scene captions only**, or pass `scene_only: true`, to require the search terms
to appear together in one caption. Results link to the matching scene or sampled 360° moment.
Leave the query empty to browse media with captions. Both search scopes work with the
local related-wording search and the existing source and date filters.

Use **Source access** in the Library to find items with a linked file currently available
on this Mac or with an unexpired, recorded Google Picker selection. The Picker option checks
the saved selection receipt. It does not contact Google or verify that a new download will
match an indexed rendition. MCP accepts the same filter as `source_access: "local"` or
`source_access: "picker"` in literal and related-wording search.

**Speech analysis** filters videos and audio by the recorded transcription outcome. Use it
to find completed transcripts, clips with no recognized words or no reported audio, failures,
and items with no recorded speech pass. A failed multipart pass can still have searchable
words from successful parts. The filter is independent of **Spoken words only**, which searches
text that is present. MCP accepts it as `speech_state` on search and related search, and
**Story from results** carries it with the other Library filters.

Google removed general existing-library read access on 31 March 2025. Its current Picker can list
only items deliberately selected in a session (up to 2,000). Session media URLs last roughly 60 minutes;
the app re-lists selected items at download time and asks for a fresh selection when access expires.
Jobs that need cloud access remain queued while their selection is missing, pending, closed, or
expired. If acquisition fails after the catalogue confirms the selection expired, FrameAtlas
cleans the partial download and returns the same job to the queue. Importing the same items
from a completed selection resumes the existing jobs with their
analysis settings and editing destinations. An available local original can still be used directly.
If a linked local file is unavailable, the job can use its Picker selection or wait for refreshed
access. FrameAtlas keeps the saved local link for reuse if the file returns.
Enable **Queue local analysis when importing** to put selected, unanalysed photos and videos with
no previous analysis job into the background queue in the same import. The MCP equivalent is
`catalog_picker_import` with `queue_analysis: true`. This option defaults off. Existing jobs retain
their settings, and failed or cancelled jobs can be retried individually.
The existing AutoCut cache is only previously selected media, not the complete Photos library.
In Sources & storage, Selection batches shows every tracked batch, its expiry and pending jobs.
It also shows how many queued jobs need Picker access and the minimum average pace to clear
those jobs across all known active deadlines. The last hour's Google analysis completions are
split into Picker downloads, local reads and jobs without a recorded acquisition source. Older
jobs without a receipt are counted separately. Picker completions and queued work also show
their photo and video counts. Each deadline shows the minimum hourly pace for each media type,
and the selection summary shows the highest required pace for each type across active deadlines.
Compare those rates with the recent mix of completions. Actual speed varies with media and other work.
Expand Selected capture months for the actual monthly counts in a selection. The date range
shows its earliest and latest captures, and coverage can have gaps within or between months.
Batch references are also available through `catalog_picker_sessions`. Keep a batch open until
its jobs finish or all its source links move to a newer selection. Reimporting an older batch
preserves an active source link with a later recorded expiry. Historical import counts remain
a receipt; current linked/analysed counts follow the active source links. Closed receipts stay
visible. The list is bounded to 200 rows with an explicit truncation notice and active batches first.
**Show queued media** lists the filenames and capture times of the files still queued for a
batch's cloud access. If access expires, use that list to choose the unfinished files in a new
Picker session. Importing the same items refreshes their locators and preserves existing jobs.
The same paginated list is available over MCP as `catalog_picker_queued_items`.
**Media waiting for Google access** lists blocked sources across all batches, including
records with a missing selection. The worklist shows why each item is waiting and groups
multiple queued jobs for the same source. MCP clients can use `catalog_picker_reselection`.

Google Picker IDs are not assumed to be Photos web-page IDs. When there is no verified product link,
the app offers a filename search link rather than inventing a direct URL.

Takeout gives a broader metadata inventory including GPS when supplied, but its IDs cannot always be
matched to Picker IDs. Records stay separate until explicitly associated/merged; filenames alone
never prove identity. Repeated direct photo permalinks identify one Takeout record. Without such
a permalink, archive name, member, position and sidecar bytes identify the exported occurrence.
An unchanged archive can be moved between folders and reimported without adding those records.
Different export occurrences can describe the same cloud item and remain separate.
For a confirmed match, use **Link a verified Takeout location** under **Correct & organise**
on the Picker item, or call `catalog_link_takeout_location`. This copies the recorded GPS
with a reference to the Takeout record and preserves both identities, analysis and jobs.
The link can be removed. Embedded GPS and existing catalogue GPS take precedence.
The importer reads existing local archives only; it does not initiate a huge
Takeout download. During still-photo analysis, FilmCraft also reads embedded EXIF coordinates
when available. Google Picker renditions can omit them. **GPS bounds** in the library and the
optional `bounds` argument to `catalog_search` accept south, west, north, east in decimal degrees.
Recorded coordinates appear in **Source & provenance**. The filter includes embedded photo GPS
and valid coordinates from imported metadata. It leaves out unknown positions and model guesses.
On macOS, build the optional reader with `./run.sh build-video-gps` to include file-level
QuickTime location metadata for supported MOV, MP4, M4V and INSV videos. It reads local files
with AVFoundation and accepts decimal-degree coordinates. Split recordings keep each part's
position and get a group position only when every part reports the same coordinates.

Insta360 discovery imports **Studio's existing local download inventory**, explicit manifests, and
reviewed Cloud Media list screenshots. The screenshot workflow uses macOS's local CPU OCR helper
or installed local Qwen2.5-VL, checks camera dates against the visible date, and preserves an evidence
hash. Camera and display timestamps are retained separately when they differ by a plausible time-zone
offset; the timezone remains unknown. OCR remains marked unverified, and unreadable rows are reported.
It covers captured visible rows; it does not automatically enumerate the cloud or claim a public cloud API.
The 8 November 2026 capture reached and repeated Studio's last page, which showed **413 files**;
overlapping pages and local OCR recovered 413 distinct filenames. This is a visible-list snapshot,
not completed scene analysis or a provider-verified library inventory.
Cloud-only analysis and editing retrieval create a private, quota-limited temporary volume on macOS.
The queue shows its destination and exact recording name. In signed-in Studio, choose Download →
Browse to that destination and leave **Set as default download path OFF**. Studio can reopen the
previous job's folder, which has the same `studio-source` name. Check the parent `job-…` name
against the active FrameAtlas job before accepting the folder, then verify the destination changed
in Studio's Download dialog. In the macOS chooser, you can navigate to the exact parent
`job-…` folder and explicitly select **FrameAtlas Temporary Source**, the mounted
`studio-source` row. Wait for Studio to report
completion, then press **Studio download finished** (or call `catalog_confirm_studio_download`).
FrameAtlas validates the exact filename, stability, native camera trailer and media probe before analysis.
The source volume has a hard limit reserved within the chosen temporary-storage cap.
In Sources & storage, choose **2, 4, 8, 16 or 32 GiB** (default 2 GiB; MCP: `catalog_storage_limit`).
The corresponding Studio source quotas are **1.5, 3.5, 7.5, 15.5 or 31.5 GiB**, leaving room for native
working images and overhead. Each job snapshots its allowance; a changed setting applies to new
jobs. Lowering below an active reservation is rejected, and the 3 GiB host free-space reserve
always applies. A recording beyond the largest quota needs another strategy or a read-only local original. Camera LRV companions are reused
when supplied. Studio currently does not offer separate cloud proxy downloads.

Google transfers also accept the chosen job allowance up to32 GiB; the current job keeps its
snapshotted allowance. Single-source lookup stops once its stable ID is found, without fetching
unneeded later selection pages. Full selection import follows pagination to its actual end and
rejects malformed pages, repeated tokens or IDs, more than2,000 items, or more than128 pages
instead of silently importing a partial prefix. See [Google’s pagination reference](https://developers.google.com/photos/picker/reference/rest/v1/mediaItems/list).

The app gracefully unmounts and removes only its confirmed, marked temporary source after the job.
It leaves a zero-byte file at the retired job path so Studio cannot recreate that path as an
ordinary download folder. If Studio remembers an old path, select the current FrameAtlas job's
destination before starting the download.
Unconfirmed source volumes survive cancellation and worker restarts, keeping their storage quota in
place until the transfer is finished/cancelled in Studio and explicitly cleaned. It never
force-unmounts a busy writer or removes an unknown mounted disk. If a transfer fails or is cancelled,
finish/cancel it in Studio before using **Clean temporary source** (MCP:
`catalog_cleanup_studio_download`). Existing Studio downloads and its default destination are untouched.
Studio selection remains an operator/agent UI step; this is not an unattended cloud API.

For 2–8 cloud-only Insta360 items, choose **Download selected items together in Studio**
in the analysis dialog, or use `catalog_stage_studio_batch`. This stages the batch ahead
of background analysis by default so the operator can start the transfer promptly.
To keep background order, clear the priority checkbox or set `options.priority=false` in MCP.
The jobs list shows the expected recording count, exact filenames and shared destination.
In Studio, click the first recording by itself to clear the previous selection, then add the
other listed recordings. Cancel the Download dialog and reselect if its count differs from
FrameAtlas. Use **Confirm this
recording** when Studio marks that file complete, or **Confirm remaining batch** after all remaining
transfers finish. The MCP equivalent for one recording is `catalog_confirm_studio_download`
with `individual: true`. Once that recording is indexed, FrameAtlas can release its unchanged
package from the shared temporary volume and show how much space it freed. If a transfer
ran out of space, retry that recording in Studio when enough space has been freed. If the
transfer must be abandoned, stop it in Studio before cancelling its FrameAtlas job.
While a single transfer or a batch awaits confirmation, the same worker can index up to four compatible Google
stills at a time. These opportunistic downloads need a reported size within 32 MiB per file
and a 20-second transfer window. The worker reserves room for the Studio volume's full quota
and the host free-space reserve. Deferred stills stay queued. If a Studio confirmation arrives
during a grouped transfer, the worker yields to it and returns the staged stills to the queue.
The shared volume is automatically cleaned once all transfers are confirmed and analysis finishes.
If a transfer was cancelled without whole-batch confirmation, stop it in Studio and clean the
temporary source from any batch job after the jobs finish.


Studio split groups such as `..._049_050.insv` are resolved only from their exact `-Original`
folder. Every distinct named part must arrive and pass native camera validation before the group
is accepted. Each part is analysed locally in order, retaining its own calibration and proxy.
Group timestamps concatenate the native durations; every scene and spoken-word match also carries
`source_part` with its exact filename and local seconds. Scene corrections use separate identities
per part. Search can find either the group or an actual part filename. Editing retrieval retains all
original parts and available camera companions together in one permanent package. It never hands
off only the first part of a group.

360° originals are imported read-only so FilmCraft retains their calibrated VR properties and gyro
horizon/heading correction where available. The native engine uses the attached LRV internally;
the app does not import an uncalibrated proxy as ordinary video. A stitched panorama supplies the
native shot descriptions. Six **512 × 512, 100°** camera views (four horizontal headings, up and down)
are also rendered at the centre of temporal windows and described with the installed local model.
The target spacing is 20 seconds, bounded to **720 windows** per source (four hours at the target
spacing). Longer videos have wider spacing, recorded explicitly in provenance. Processing streams
**16 windows / 96 frames per batch**: after local descriptions are read, the batch is removed from
the native media pool and disk before the next batch. Only one preview frame is retained across
batches. The maximum temporary directional frame allowance is **194 MiB**, within the 256 MiB
analysis reserve. Provenance records both cumulative bytes rendered and peak bytes held. A 360
photo is sampled once in six directions.

When the native engine supplies no VR properties for a source marked 360°, the catalogue can
retain a searchable source-frame description as partial. It shows a placeholder until a calibrated
directional preview is available. A real cloud INSP photo exercised this path.

These are timecoded observations at sampled moments, **not continuous verification of every frame**.
Records remain partial with an explicit coverage warning. Search points to the actual requested sample
time and returns camera angles; story evidence distinguishes the sample time from its surrounding
window. The original native shot records are retained, and each directional window has its own edit
identity. Saved corrected scenes survive a later change of scene boundaries. Temporary perspective
images are removed after the job; only descriptions and a small calibrated JPEG preview remain.
Preview replacement is atomic and retains the previous image if conversion fails.

Large-source metadata is read through FilmCraft’s native streaming reader, including a disposable
read-only alias for extensionless sources. The app does not load a complete camera file into memory
merely to probe it. No source bytes are copied by the alias.

Named recordings take priority when assembling local story evidence. An explicit “only” request
uses only the analysed named sources. Evidence and replies are bounded, replies use a JSON schema,
and incomplete local replies return an actionable error without saving a partial idea. Split-group
citations include the original filename and local seconds, alongside the group time.
Directional evidence can be cited only at its observed sample time. A range needs an ordinary
native shot. Story generation and optional Ollama OCR connect directly to the loopback service
without using HTTP proxies or following redirects.

Both library search modes use SQLite full-text search. The optional local model expands the wording
before lookup. The catalogue does not store or search embedding vectors.
Scene segmentation is shot-based: one long uncut shot can contain changing activities. Descriptions,
OCR guesses, tags and transcripts require review. The app does not identify people by face or invent
landmark names. Story prose is brainstorming, never verified narration.

Official references:

- [Google Photos API changes](https://developers.google.com/photos/support/updates)
- [Picker media and download lifetime](https://developers.google.com/photos/picker/guides/media-items)
- [Insta360 cloud viewing](https://onlinemanual.insta360.com/insta360%2B/en-us/operating-tutorials/operation-instructions/view-media)
- [Insta360 download/proxy limitations](https://onlinemanual.insta360.com/insta360%2B/en-us/operating-tutorials/operation-instructions/clips-download)
- [Ollama local structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
- [Apple Vision text-recognition API](https://developer.apple.com/documentation/vision/vnrecognizetextrequest)

## Local Studio text reader

Build the optional macOS CPU helper once with `./run.sh build-ocr` (requires the installed Apple
command-line tools). No package, model or network download is performed. Sources & storage offers
the CPU reader and the existing local Qwen vision model. Choose Cloud Media in Studio, use detailed
list view, capture both filename/date columns, read the screenshot, then review before importing.
The CPU reader checks visible labels, not the selected tab's highlight: verify Cloud Media yourself.

For an LLM, `catalog_studio_ocr` accepts `engine: "apple_vision"` or `"qwen"` and a screenshot path.
`catalog_import_studio_inventory` saves a reviewed preview. `catalog_import_studio_captures` imports
up to 150 overlapping previews atomically and deduplicates files while preserving per-file evidence.
No original videos are acquired by these operations. The CPU helper uses Apple's system Vision
framework, runs in a separate bounded process, and leaves the video-analysis GPU available.

## Fresh Google selections

1. Sources & storage → Choose from Google Photos. Open the Picker link and select media.
2. Press Done in Google, then Import selected metadata in FrameAtlas.
3. Find/select those records in the library and queue analysis promptly.
4. Once the jobs finish, close the finished Picker session in Sources.

When Google says the saved sign-in expired, run the existing secure broker flow:

```sh
filmcraft-cli photos auth
```

Complete consent before its five-minute callback timeout. Credentials stay in FilmCraft's existing
secure vault. The browser and Python service never read or copy credential files.

## Retrieve a source for editing

Use **Copy clip reference** on an indexed scene to copy its stable source identity and exact time
in seconds. For a split Insta360 recording the reference gives the original part filename and
the time within that file alongside the catalogue time. A 360° sample carries a single moment.
MCP clients can pass the current `scene_key` and optional `part_filename` to `catalog_resolve`
for the same information, including a source path when the original is available.
For an exact transcript search match, use **Copy spoken clip reference** or pass its
`speech_key` to `catalog_resolve`. The resolver checks the current words and timing.
Copying either reference leaves the media and download queue untouched.

Select two records and use **Compare source bytes**, or call `catalog_compare_sources`
with their two catalogue IDs. FrameAtlas records a SHA-256 fingerprint while an analysed
single-file source is available. The comparison reports identical bytes, different bytes,
or missing evidence. Older analysis can lack a fingerprint, and multi-file camera packages
are excluded. Identical bytes describe the files read during analysis. Cloud identities
and their retrieval links remain separate.

Open a media detail → **Prepare for editor** → Queue priority retrieval. If the original is already
available locally, its full path is returned read-only; no extra copy is made. Camera LRV files are
related dependencies, never substituted for the full Insta360 original in an editing handoff.

For a Google cloud-only selection, first choose it in the Picker, then enter an existing editing
folder outside FrameAtlas data/cache. The editing panel and `catalog_resolve` report whether the
saved selection needs refreshing. Legacy locators without a selection receipt show unknown access.
Only that selected file is downloaded, under the selected
temporary-storage budget, then published without overwriting any existing file. The editing source is retained
at that chosen destination and is never expired automatically. Temporary acquisition files are
removed. On filesystems without hard-link support, cross-volume publishing fails safely; use a
local APFS editing folder. Missing/expired selections and insufficient space give actionable errors.

Retrieval jobs run before the remaining analysis backlog, after the active job or scene batch finishes. The queue
shows completed editing paths ahead of pending analysis; MCP clients call `catalog_retrieve` and then
`catalog_job` with the returned job ID. To carry exact edit times into the completed job, use
**Prepare this scene for editor** or **Prepare this match for editor**. MCP clients can pass the
same `scene_key` and optional `part_filename`, or `speech_key`, to `catalog_retrieve`.
The job's `clip` reports the range or 360° sample point and the actual editor path for the
matching source file. Retrieval still supplies the complete original or camera package.
On a completed queue card, use **Copy editor path** for a whole file or **Copy clip source path**
for the exact referenced file. **Copy timed reference** includes that path, source seconds
and the job's verification status.
When a fingerprint exists, timed retrieval verifies the file before publishing it and
reports `indexed_source_verification: matched`. For split camera recordings it verifies
the part named by the timed reference. The rest of the package is supplied together.
A mismatch stops the handoff. Older analyses or parts without a fingerprint still report
`no_fingerprint` for exact byte verification. Timed retrieval checks any available recorded
file size, media kind, duration and frame dimensions against the referenced file. A known
difference stops the handoff before publication. The result lists matched properties in
`indexed_source_properties_checked`, and the queue displays them separately from the checksum status.
Whole-file Google Photos retrieval also checks an available fingerprint. It can deliver a
different rendition on request, reports `indexed_source_verification: different_bytes`, and
keeps the catalogue source link as it was. Use full analysis on the new file to index that rendition.
For a timed Google Photos handoff with a linked local file, an editing folder is optional.
If the linked file fails verification and the folder is supplied, FrameAtlas tries the
selected cloud copy and applies the same checks before publishing it. A successful fallback
records `local_source_mismatch_recovered` and leaves the previous file untouched.
Pending analysis of that same asset remains intact. A source
that is only on Insta360 Cloud still needs the recorded-name Studio download workflow described above.

For up to eight items, pass `options.interactive=true` to `catalog_analyze` to advance them ahead
of ordinary priority analysis. The UI uses this for small selections when the priority box is
checked. The active job or scene batch finishes first. Editor retrieval, speech retries and
priority cloud-only Insta360 staging retain precedence.
For larger selections, use `options.priority=true` for ordinary priority. Both options can
promote existing queued work while preserving its analysis settings and job ID.
The queue separates failures needing attention from attempts followed by a successful job.
Historical job receipts retain the original error and report the successful job as `recovered_by`.
An editing retrieval must succeed for the same destination, and any earlier temporary Studio
source still needing cleanup keeps that failed attempt visible.
Within each analysis priority, cloud-only Google Photos jobs use the earliest known
selection deadline first. Local originals and selections without a recorded deadline remain
queued for later processing.
Consecutive Google Photos analysis jobs reuse a single Picker selection in the native broker's
memory for up to 20 minutes. The broker reads pages until it finds the requested files and
resumes from the next page for later requests. A new selection or access token starts a fresh
lookup. Importing a selection still reads its complete inventory. Each job downloads only its
own bounded temporary source. When a Google Photos still reaches the head
of the queue, the worker can group up to thirty-two compatible stills from the next thirty-two entries within
the same selection, options and expiry priority. The broker can fetch up to eight stills at once.
Each still needs a reported size within 32 MiB, and the worker reserves every file's allowance
plus the analysis reserve before starting the transfer. Smaller available budgets can use narrower
groups, and earlier brokers keep using their advertised width.
Up to six compatible Google videos can transfer in parallel if each has a reported size
within the broker's allowance (up to 256 MiB) and the cache can reserve every allowance
plus the analysis reserve. Six transfers can reserve up to 1.5 GiB for sources and
256 MiB for analysis. Smaller available budgets and earlier brokers use narrower groups.
A parallel video request waits up
to 45 seconds for response headers per HTTP request. If that wait expires, the worker analyses
the completed peers before retrying the slow source on its own, with the ordinary acquisition
time allowance. The header limit does not cap the time available to receive the media body.
Downloaded peers count against the cache budget. A peer that fails its own checks stays queued.
When the broker defers a source from a parallel request, the job remembers that allowance
and skips another parallel request at the same or a smaller limit.
Up to three compatible Google Photos videos can share a local scene pass
if each is flat, at most 20 seconds and 32 MiB, and no larger than UHD in either orientation.
The native media probe still checks these criteria. Longer, larger, 360° and multipart sources
run individual analysis. Eligible short videos can share one local speech call while retaining
their own transcript and word timings. Clips that need audio windows or lack a usable grouped
transcript use the individual path. Each clip keeps its own scenes and preview. Every job
owns and cleans its temporary source under the shared storage limit. Editor retrieval keeps its
separate one-shot broker path.
During local analysis of a Picker source, the worker can stage one compatible bulk batch ahead.
If the next video has no compatible peer or was deferred by a parallel request, it can instead
stage that video on its own. The standalone request needs a reported size within the same
per-video allowance and uses the ordinary header wait within the overall lookahead time limit.
Lookahead reserves every transfer allowance plus the active analysis reserve and has a 75-second
time limit. It uses the same file checks and does not run native inference in parallel. Pausing,
higher-priority work or reduced capacity returns staged jobs to the ordinary queue and removes
their owned downloads. Verified peers can finish analysis from the files already staged
when a photo or video is deferred or the lookahead transfer time runs out. Unfinished
members return to the ordinary queue. The worker checks priority again before analyzing
the staged group. Still batches can retain all thirty-two items for the native pass.
If the broker stops before replying, the worker can keep completed members of that transfer
wave whose owned files have a matching byte-count receipt. The broker publishes each file only
after finishing and syncing its body. Partial files and files without a receipt are discarded.
The broker retries temporary Google media server errors at most twice before opening a local
destination. It observes short numeric retry delays and reports errors that persist.
Bulk speech analysis validates the selected local model through FilmCraft when it loads it.
If the selected model cannot run, the catalogue retains the scene index and marks the
speech pass failed so it can be retried.
New Google analysis job receipts report measured stage times in `result.phase_seconds`
through `catalog_job`. Times are per job. Shared scene passes overlap between batch members,
and preparation can include waiting for other members to acquire their sources.

For a failed transcript or a pass that recognized no words on a single indexed file,
use **Retry transcript** in the Transcript tab
or `catalog_retry_speech` to choose the language and installed model explicitly. This job runs
ahead of the analysis backlog. It verifies the source against the indexed size, duration,
streams and available fingerprint, then updates speech and search without rewriting
existing scenes, previews or edits.

For a split Insta360 recording with a failed part, use **Retry failed parts** or the same MCP
tool. Studio supplies the complete package in the temporary volume. FrameAtlas verifies the
indexed rendition of each failed part, transcribes just those parts, and preserves the timed
words from successful parts. The package is removed after the retry.

Use **Re-transcribe locally** on a completed transcript to try a different language or model,
or pass `replace: true` to the same MCP tool. The existing transcript stays searchable until a
replacement with timed words succeeds. Malayalam replacements suggest the installed
Whisper Medium Malayalam model.

## Native cloud broker build

```sh
./run.sh build-cloud
```

The broker depends only on the sibling FilmCraft OAuth crate and a small HTTPS/JSON stack; it does
not rebuild editor/codec crates. It uses FilmCraft's secure vault API and OAuth refresh implementation.
The Python service and browser never handle credentials. The native broker reads the existing OAuth
client configuration only when a refresh is needed, and never returns or logs tokens. Binary hashes
are recorded alongside the private executable. Builds use cached Cargo dependencies offline. Rebuild
the broker after upgrading FilmCraft's OAuth API or Rust.

## MCP and CLI

Use `.mcp.json` in an MCP-capable client, or configure:

```json
{"command":"/absolute/path/to/FrameAtlas/run.sh","args":["mcp"]}
```

MCP queues share the same database. Keep `./run.sh serve` or the separate `./run.sh worker` running
for processing. Tool names include
`catalog_search`, `catalog_asset`, `catalog_analyze`, `catalog_jobs`, `catalog_resolve`,
`catalog_correct_scene`, `catalog_ideas`, `catalog_import_takeout`, `catalog_picker_create`,
`catalog_metadata_coverage`, and the Idea Pad tools `idea_pad_list`, `idea_pad_add`,
`idea_pad_update`, `idea_pad_delete`, `idea_pad_moments`, `idea_pad_generate` and
`idea_pad_develop` (`catalog_ideas` saves its stories to the pad too).
`catalog_search` exposes `limit` (1–200) and `offset` for paging through the whole known inventory.

```sh
./run.sh import-native
python3 -m frameatlas.cli search "hotel"
python3 -m frameatlas.cli import-takeout /path/to/takeout.zip
python3 -m frameatlas.cli stats
python3 -m frameatlas.cli metadata-coverage
python3 -m frameatlas.cli ideas-generate --count 5 --since 2024-01-01 "360"
python3 -m frameatlas.cli ideas
./run.sh test
```

FrameAtlas finds the AutoCut CLI through `FILMCRAFT_CLI`, then `filmcraft-cli` on `PATH`, then a
sibling `../Autocut` checkout's release build. Override `FILMCRAFT_CLI` / `FRAMEATLAS_CLOUD_BRIDGE`
to use compatible native builds elsewhere.

## Security and data ownership

Loopback binding only, exact Host validation, Origin and fetch-site checks, per-run CSRF token,
strict static/thumbnail routes, no arbitrary-file HTTP endpoint, no external scripts, and a restrictive
CSP. Media/path associations require an explicit local UI/MCP request. Full originals are never served
through the web app. The Rust broker accepts only Google media hosts over HTTPS, refuses credentialed
redirects, streams under a byte cap into an owned cache job, and keeps authorization entirely inside
the existing native vault. Do not expose this personal single-user development server to a LAN or Internet.

The optional native broker is published by staging and atomically replacing its executable;
build/copy/provenance failures before publication keep the existing broker intact. Already-open
processes retain the earlier executable. Build provenance hashes are computed with bounded reads.

Original UI/code are created for this project. Fonts are system fonts; there are no third-party artwork
assets. User media, metadata databases, model weights, credentials and compiled dependencies are excluded
from git. FilmCraft, model and dependency licenses remain their respective upstream licenses.
