//! Credential broker: reuse FilmCraft's secure OAuth vault; never expose a token or signed URL.
use std::{
    collections::HashSet,
    env, fs,
    io::{self, BufRead, Read, Write},
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};
mod photos_api;
use photos_api::{GooglePhotos, Item, MediaKind, SelectionItems};
use serde_json::{Value, json};

type Error = Box<dyn std::error::Error>;
const MAX_DOWNLOAD_BYTES: u64 = 32 * 1024 * 1024 * 1024;
const MAX_PAIRED_SOURCE_BYTES: u64 = 32 * 1024 * 1024;
const MAX_PAIRED_VIDEO_BYTES: u64 = 256 * 1024 * 1024;
const MAX_PARALLEL_PHOTOS: usize = 8;
const MAX_PARALLEL_VIDEOS: usize = 6;
const MAX_REQUEST_BYTES: u64 = 16 * 1024;
const SELECTION_CACHE_AGE: Duration = Duration::from_secs(20 * 60);
const PARALLEL_VIDEO_HEADER_WAIT: Duration = Duration::from_secs(45);
const MEDIA_RETRY_DELAYS: [u64; 2] = [1, 3];
const TRANSFER_PART: &str = ".frameatlas-transfer";
const TRANSFER_COMPLETE: &str = ".frameatlas-transfer-complete";
fn download_cap(value: &str) -> Result<u64, Error> {
    let bytes: u64 = value.parse()?;
    if bytes == 0 || bytes > MAX_DOWNLOAD_BYTES {
        return Err("Download cap must be 1 byte–32 GiB, within the selected job allowance".into());
    }
    Ok(bytes)
}
fn argument(args: &[String], n: usize) -> Result<&str, Error> {
    args.get(n)
        .map(String::as_str)
        .filter(|s| !s.is_empty() && s.len() < 4096)
        .ok_or_else(|| "Missing or invalid argument".into())
}
fn media_host(url: &str, redirect: bool) -> bool {
    let Some(rest) = url.strip_prefix("https://") else {
        return false;
    };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or_default();
    if authority.contains('@') || authority.contains('%') {
        return false;
    }
    let mut parts = authority.split(':');
    let host = parts.next().unwrap_or_default().to_ascii_lowercase();
    if let Some(port) = parts.next()
        && port != "443"
    {
        return false;
    }
    if parts.next().is_some() {
        return false;
    }
    let domains: &[&str] = if redirect {
        &[
            "googleusercontent.com",
            "googlevideo.com",
            "googleapis.com",
            "google.com",
        ]
    } else {
        &["googleusercontent.com", "googleapis.com", "google.com"]
    };
    domains
        .iter()
        .any(|d| host == *d || host.strip_suffix(d).is_some_and(|sub| sub.ends_with('.')))
}
fn validate_destination(dest: &Path) -> Result<(), Error> {
    let root = env::var_os("FRAMEATLAS_DATA")
        .map(PathBuf::from)
        .ok_or("FRAMEATLAS_DATA must be set")?
        .join("cache")
        .canonicalize()?;
    let parent = dest
        .parent()
        .ok_or("Invalid staging path")?
        .canonicalize()?;
    let marker = parent.join(".frameatlas-owned");
    if parent.parent() != Some(root.as_path())
        || !parent
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with("job-"))
        || !fs::symlink_metadata(marker).is_ok_and(|meta| meta.is_file())
        || dest
            .file_name()
            .is_none_or(|name| name == TRANSFER_PART || name == TRANSFER_COMPLETE)
        || dest.exists()
    {
        return Err("Destination must be a new file in an owned cache job".into());
    }
    Ok(())
}

fn publish_body(body: impl Read, dest: &Path, max: u64, length: Option<u64>) -> Result<u64, Error> {
    let partial = dest.with_file_name(TRANSFER_PART);
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&partial)?;
    let published = (|| -> Result<u64, Error> {
        let n = io::copy(&mut body.take(max.saturating_add(1)), &mut file)?;
        if n == 0 || n > max || length.is_some_and(|expected| n != expected) {
            return Err("Download interrupted, empty, or exceeded its cap".into());
        }
        file.sync_all()?;
        // An interrupted broker can leave a partial file, but cannot expose
        // it at the destination. Hard-link publication never overwrites.
        fs::hard_link(&partial, dest)?;
        Ok(n)
    })();
    drop(file);
    let cleaned = fs::remove_file(&partial);
    let n = published?;
    cleaned?;
    let mut receipt = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(dest.with_file_name(TRANSFER_COMPLETE))?;
    writeln!(receipt, "{n}")?;
    Ok(n)
}

fn retry_media_response<T>(
    retries: &mut usize,
    mut request: impl FnMut() -> Result<ureq::http::Response<T>, Error>,
    mut wait: impl FnMut(Duration),
) -> Result<ureq::http::Response<T>, Error> {
    loop {
        let response = request()?;
        let Some(&delay) = MEDIA_RETRY_DELAYS.get(*retries) else {
            return Ok(response);
        };
        if !matches!(response.status().as_u16(), 500 | 502 | 503 | 504) {
            return Ok(response);
        }
        let delay = if let Some(value) = response.headers().get("retry-after") {
            let Some(seconds) = value
                .to_str()
                .ok()
                .and_then(|text| text.trim().parse::<u64>().ok())
                .filter(|&seconds| seconds <= 30)
            else {
                return Ok(response);
            };
            delay.max(seconds)
        } else {
            delay
        };
        drop(response);
        *retries += 1;
        wait(Duration::from_secs(delay));
    }
}

fn media_response(
    agent: &ureq::Agent,
    url: &str,
    auth: Option<&str>,
    header_wait: Option<Duration>,
) -> Result<ureq::http::Response<ureq::Body>, ureq::Error> {
    let request = agent.get(url);
    let request = if let Some(auth) = auth {
        request.header("Authorization", auth)
    } else {
        request
    };
    request
        .config()
        .max_redirects(0)
        .timeout_global(Some(Duration::from_secs(1800)))
        .timeout_recv_response(header_wait)
        .build()
        .call()
}

fn download(
    photos: &GooglePhotos,
    item: &Item,
    dest: &Path,
    max: u64,
    require_known_size: bool,
    header_wait: Option<Duration>,
) -> Result<Value, Error> {
    if !media_host(&item.base_url, false) {
        return Err("Google did not supply an allowed download host".into());
    }
    if item.video_status.as_deref().is_some_and(|s| s != "READY") {
        return Err("Google video is not ready to download".into());
    }
    let mut url = format!(
        "{}={}",
        item.base_url,
        if item.kind == MediaKind::Video {
            "dv"
        } else {
            "d"
        }
    );
    let auth = format!("Bearer {}", photos.access_token()?);
    // Follow a bounded set of Google-owned HTTPS media redirects manually. The
    // bearer token is attached only to the initial Picker media URL, never forwarded.
    let mut ready = None;
    let mut retries = 0;
    for hop in 0..6 {
        if !media_host(&url, hop > 0) {
            return Err("Download redirect left Google's media hosts".into());
        }
        let response = retry_media_response(
            &mut retries,
            || {
                media_response(
                    &photos.agent(),
                    &url,
                    (hop == 0).then_some(auth.as_str()),
                    header_wait,
                )
                .map_err(|error| {
                    if header_wait.is_some()
                        && matches!(error, ureq::Error::Timeout(ureq::Timeout::RecvResponse))
                    {
                        return Box::new(error) as Error;
                    }
                    "Cloud download failed or selection expired; select this item again".into()
                })
            },
            std::thread::sleep,
        )?;
        let status = response.status().as_u16();
        if status == 200 {
            ready = Some(response);
            break;
        }
        if matches!(status, 301 | 302 | 303 | 307 | 308) {
            let location = response
                .headers()
                .get("location")
                .and_then(|v| v.to_str().ok())
                .ok_or("Google redirect has no destination")?;
            if location.starts_with('/') && !location.starts_with("//") {
                let authority = url
                    .strip_prefix("https://")
                    .and_then(|v| v.split('/').next())
                    .ok_or("Invalid source host")?;
                url = format!("https://{authority}{location}");
            } else {
                url = location.to_string();
            }
        } else {
            let message = if retries == 0 {
                format!("Unexpected cloud download status {status}")
            } else {
                format!("Unexpected cloud download status {status} after {retries} retries")
            };
            return Err(message.into());
        }
    }
    let response = ready.ok_or("Too many cloud media redirects")?;
    let length = response
        .headers()
        .get("content-length")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok());
    // A batch candidate is optional. Leave it queued without opening a local
    // file if this response cannot prove it fits the candidate budget.
    if require_known_size && !length.is_some_and(|n| n > 0 && n <= max) {
        return Ok(json!({"deferred":true,"server_retries":retries}));
    }
    if length.is_some_and(|n| n > max) {
        return Err("Source exceeds the temporary storage budget".into());
    }
    let n = publish_body(response.into_body().into_reader(), dest, max, length)?;
    Ok(json!({"bytes":n,"filename":item.filename,"temporary":true,"server_retries":retries}))
}

struct SelectionCache {
    session: String,
    loaded: Instant,
    items: SelectionItems,
    photos: GooglePhotos,
}

type DownloadRequest = (String, String, String, u64, bool);

fn download_request(line: &[u8]) -> Result<DownloadRequest, Error> {
    let value: Value =
        serde_json::from_slice(line).map_err(|_| "Invalid broker download request")?;
    if value.as_array().is_some_and(|items| items.len() == 4) {
        let (session, id, dest, max) =
            serde_json::from_value::<(String, String, String, u64)>(value)
                .map_err(|_| "Invalid broker download request")?;
        Ok((session, id, dest, max, false))
    } else {
        serde_json::from_value(value).map_err(|_| "Invalid broker download request".into())
    }
}

fn validated_destination(request: &DownloadRequest) -> Result<PathBuf, Error> {
    let (session, id, dest, max, _) = request;
    if session.is_empty()
        || session.len() > 1024
        || id.is_empty()
        || id.len() > 1024
        || dest.is_empty()
        || dest.len() >= 4096
        || *max == 0
        || *max > MAX_DOWNLOAD_BYTES
    {
        return Err("Invalid download identity, destination or byte allowance".into());
    }
    let dest = PathBuf::from(dest);
    validate_destination(&dest)?;
    Ok(dest)
}

fn load_selection(
    session: &str,
    wanted: &[&str],
    cache: &mut Option<SelectionCache>,
) -> Result<usize, Error> {
    let photos = GooglePhotos::configured()?;
    let reused = cache.as_ref().is_some_and(|entry| {
        entry.session == session
            && entry.loaded.elapsed() < SELECTION_CACHE_AGE
            && entry.photos.same_access_token(&photos)
    });
    if !reused {
        *cache = Some(SelectionCache {
            session: session.to_owned(),
            loaded: Instant::now(),
            items: SelectionItems::new(),
            photos,
        });
    }
    let entry = cache.as_mut().ok_or("Picker selection was not loaded")?;
    let pages = entry
        .items
        .find(wanted, |next| entry.photos.selection_page(session, next));
    if pages.is_err() {
        *cache = None;
    }
    pages
}

fn cached_download(line: &[u8], cache: &mut Option<SelectionCache>) -> Result<Value, Error> {
    let request = download_request(line)?;
    let dest = validated_destination(&request)?;
    let (session, id, _, max, require_known_size) = request;
    let pages = load_selection(&session, &[&id], cache)?;
    let entry = cache.as_ref().ok_or("Picker selection was not loaded")?;
    let item = entry
        .items
        .get(&id)
        .ok_or("Item not in this Picker session; select it again")?;
    match download(&entry.photos, item, &dest, max, require_known_size, None) {
        Ok(mut result) => {
            result["picker_lookup"] = json!(if pages == 0 { "reused" } else { "listed" });
            result["picker_pages"] = json!(pages);
            Ok(result)
        }
        Err(err) => {
            *cache = None;
            Err(err)
        }
    }
}

fn parallel_requests(value: Value) -> Result<Vec<DownloadRequest>, Error> {
    let width = value.as_array().map_or(0, Vec::len);
    if !(2..=MAX_PARALLEL_PHOTOS).contains(&width) {
        return Err("Expected two to eight size-gated media downloads".into());
    }
    let requests: Vec<DownloadRequest> = serde_json::from_value(value)?;
    let session = &requests.first().ok_or("Missing media downloads")?.0;
    let cap = if width <= MAX_PARALLEL_VIDEOS {
        MAX_PAIRED_VIDEO_BYTES
    } else {
        MAX_PAIRED_SOURCE_BYTES
    };
    let mut ids = HashSet::new();
    let mut paths = HashSet::new();
    for (selection, id, path, max, known_size) in &requests {
        if selection != session
            || !known_size
            || *max == 0
            || *max > cap
            || !ids.insert(id)
            || !paths.insert(path)
        {
            return Err(
                "Parallel sources must be distinct, size-gated media from one Picker selection"
                    .into(),
            );
        }
    }
    Ok(requests)
}

fn cached_pair(value: Value, cache: &mut Option<SelectionCache>) -> Result<Value, Error> {
    let requests = parallel_requests(value)?;
    let first = requests.first().ok_or("Missing media downloads")?;
    let mut destinations = Vec::new();
    let mut parents = HashSet::new();
    for request in &requests {
        let path = validated_destination(request)?;
        let parent = path
            .parent()
            .ok_or("Missing staging directory")?
            .canonicalize()?;
        if !parents.insert(parent) {
            return Err("Parallel sources need separate owned staging directories".into());
        }
        destinations.push(path);
    }
    let wanted: Vec<_> = requests.iter().map(|request| request.1.as_str()).collect();
    let pages = load_selection(&first.0, &wanted, cache)?;
    let entry = cache.as_ref().ok_or("Picker selection was not loaded")?;
    let media = |id: &str| {
        entry
            .items
            .get(id)
            .filter(|item| item.kind != MediaKind::Unknown)
            .ok_or("Media not in this Picker selection or unsupported")
    };
    let kind = &media(&first.1)?.kind;
    if *kind == MediaKind::Video && requests.len() > MAX_PARALLEL_VIDEOS {
        return Err("At most six videos can transfer in parallel".into());
    }
    let mut sources = Vec::new();
    for (request, path) in requests.iter().zip(&destinations) {
        let item = media(&request.1)?;
        if &item.kind != kind {
            return Err("Parallel sources must have the same media kind".into());
        }
        if *kind != MediaKind::Video && request.3 > MAX_PAIRED_SOURCE_BYTES {
            return Err("Parallel photos must each be capped at 32 MiB".into());
        }
        sources.push((item, path, request.3));
    }
    let header_wait = (*kind == MediaKind::Video).then_some(PARALLEL_VIDEO_HEADER_WAIT);
    let transfer = |item, path: &Path, cap| {
        match download(&entry.photos, item, path, cap, true, header_wait) {
            Ok(result) => result,
            Err(error)
                if matches!(
                    error.downcast_ref::<ureq::Error>(),
                    Some(ureq::Error::Timeout(ureq::Timeout::RecvResponse))
                ) =>
            {
                // No body was opened. Keep the successful peers and let this item retry alone.
                json!({"deferred": true, "reason": "slow_video_headers"})
            }
            Err(error) => json!({"error": error.to_string()}),
        }
    };
    let pair = thread::scope(|scope| {
        let handles: Vec<_> = sources
            .into_iter()
            .map(|(item, path, cap)| {
                thread::Builder::new().spawn_scoped(scope, move || transfer(item, path, cap))
            })
            .collect();
        handles
            .into_iter()
            .map(|handle| match handle {
                Ok(handle) => handle
                    .join()
                    .unwrap_or_else(|_| json!({"error": "Parallel download failed safely"})),
                Err(_) => json!({"error": "Could not start a parallel download"}),
            })
            .collect::<Vec<_>>()
    });
    if pair.iter().any(|result| result.get("error").is_some()) {
        *cache = None;
    }
    Ok(
        json!({"pair": pair, "picker_lookup": if pages == 0 { "reused" } else { "listed" }, "picker_pages": pages}),
    )
}

fn download_loop(mut input: impl BufRead, mut output: impl Write) -> Result<(), Error> {
    let mut cache = None;
    let mut line = Vec::new();
    loop {
        line.clear();
        let n = (&mut input)
            .take(MAX_REQUEST_BYTES + 1)
            .read_until(b'\n', &mut line)?;
        if n == 0 {
            return Ok(());
        }
        if n as u64 > MAX_REQUEST_BYTES || !line.ends_with(b"\n") {
            return Err("Broker request exceeded its framing limit".into());
        }
        let result = if line.first() == Some(&b'{') {
            serde_json::from_slice::<Value>(&line)
                .map_err(|_| "Invalid paired broker download request".into())
                .and_then(|mut value| {
                    let pair = value.get_mut("pair").ok_or("Missing paired downloads")?;
                    cached_pair(pair.take(), &mut cache)
                })
        } else {
            cached_download(&line, &mut cache)
        };
        let value = match result {
            Ok(value) => value,
            Err(error) => json!({"error":error.to_string()}),
        };
        writeln!(output, "{value}")?;
        output.flush()?;
    }
}

fn run(args: &[String]) -> Result<Value, Error> {
    let action = argument(args, 1)?;
    if action == "capabilities" {
        return Ok(
            json!({"provider":"google_photos","inventory":"user-selected Picker sessions","credentials":"FilmCraft secure vault","returns_download_urls":false,"maximum_download_bytes":MAX_DOWNLOAD_BYTES,"persistent_downloads":true,"known_size_batch_downloads":true,"paired_photo_downloads":true,"maximum_parallel_photos":MAX_PARALLEL_PHOTOS,"paired_video_downloads":true,"maximum_parallel_videos":MAX_PARALLEL_VIDEOS,"maximum_paired_video_bytes":MAX_PAIRED_VIDEO_BYTES}),
        );
    }
    let photos = GooglePhotos::configured()?;
    match action {
        "create" => {
            let s = photos.create_session(Some(2000))?;
            Ok(json!({"id":s.id,"picker_uri":s.picker_uri,"expires":s.expire_time}))
        }
        "list" => {
            let session = argument(args, 2)?;
            let s = photos.session(session)?;
            if !s.media_items_set {
                return Ok(json!({"ready":false,"items":[]}));
            }
            let items: Vec<Value> = photos.picked_items(session)?.iter().map(|i| json!({"id":i.id,"filename":i.filename,"kind":i.kind.as_str(),"created":i.created,"mimeType":i.mime_type,"capture":i.capture})).collect();
            Ok(json!({"ready":true,"items":items,"session":session}))
        }
        "delete" => {
            photos.delete_session(argument(args, 2)?)?;
            Ok(json!({"ok":true}))
        }
        "download" => {
            let session = argument(args, 2)?;
            let id = argument(args, 3)?;
            let dest = PathBuf::from(argument(args, 4)?);
            let max = download_cap(argument(args, 5)?)?;
            validate_destination(&dest)?;
            let item = photos
                .picked_item(session, id)?
                .ok_or("Item not in this Picker session; select it again")?;
            download(&photos, &item, &dest, max, false, None)
        }
        _ => Err("Unknown broker operation".into()),
    }
}
fn main() {
    let args: Vec<String> = env::args().take(10).collect();
    if args
        .get(1)
        .is_some_and(|action| action == "serve-downloads")
    {
        let result =
            std::panic::catch_unwind(|| download_loop(io::stdin().lock(), io::stdout().lock()));
        if !matches!(result, Ok(Ok(()))) {
            eprintln!("Cloud download broker stopped safely");
            std::process::exit(1);
        }
        return;
    }
    let result = std::panic::catch_unwind(|| run(&args));
    let (value, failed) = match result {
        Ok(Ok(v)) => (v, false),
        Ok(Err(e)) => (json!({"error":e.to_string()}), true),
        Err(_) => (
            json!({"error":"Cloud broker failed safely; retry the operation"}),
            true,
        ),
    };
    println!("{value}");
    if failed {
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn download_publication_requires_a_complete_body_and_preserves_existing_files()
    -> Result<(), super::Error> {
        use std::{
            fs,
            time::{SystemTime, UNIX_EPOCH},
        };

        let folder = std::env::temp_dir().join(format!(
            "frameatlas-broker-test-{}-{}",
            std::process::id(),
            SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos(),
        ));
        fs::create_dir(&folder)?;
        let checked = (|| -> Result<(), super::Error> {
            let dest = folder.join("source.jpg");
            let partial = folder.join(super::TRANSFER_PART);
            let receipt = folder.join(super::TRANSFER_COMPLETE);
            assert!(super::publish_body(&b"short"[..], &dest, 10, Some(6)).is_err());
            assert!(!dest.exists() && !partial.exists() && !receipt.exists());
            assert!(super::publish_body(&b"too long"[..], &dest, 3, None).is_err());
            assert!(!dest.exists() && !partial.exists() && !receipt.exists());
            assert_eq!(super::publish_body(&b"good"[..], &dest, 10, Some(4))?, 4);
            assert_eq!(fs::read(&dest)?, b"good");
            assert_eq!(fs::read(&receipt)?, b"4\n");
            assert!(!partial.exists());
            assert!(super::publish_body(&b"replacement"[..], &dest, 20, Some(11)).is_err());
            assert_eq!(fs::read(&dest)?, b"good");
            Ok(())
        })();
        fs::remove_dir_all(folder)?;
        checked
    }
    #[test]
    fn speculative_header_wait_does_not_change_individual_requests() -> Result<(), super::Error> {
        use std::io::{Read, Write};
        use std::net::TcpListener;
        use std::thread;

        for header_wait in [Some(Duration::from_millis(40)), None] {
            let listener = TcpListener::bind("127.0.0.1:0")?;
            let url = format!("http://{}", listener.local_addr()?);
            let server = thread::spawn(move || -> std::io::Result<()> {
                let (mut stream, _) = listener.accept()?;
                stream.set_read_timeout(Some(Duration::from_secs(2)))?;
                stream.read_exact(&mut [0])?;
                thread::sleep(Duration::from_millis(150));
                let sent = stream.write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                );
                if header_wait.is_none() {
                    sent?;
                }
                Ok(())
            });
            let agent = ureq::Agent::config_builder()
                .proxy(None)
                .build()
                .new_agent();
            let response = super::media_response(&agent, &url, None, header_wait);
            if header_wait.is_some() {
                assert!(matches!(
                    response,
                    Err(ureq::Error::Timeout(ureq::Timeout::RecvResponse))
                ));
            } else {
                assert_eq!(response?.status(), 200);
            }
            server.join().map_err(|_| "Local test server failed")??;
        }
        Ok(())
    }

    #[test]
    fn persistent_download_protocol_rejects_invalid_frames_without_exposing_data()
    -> Result<(), super::Error> {
        let mut out = Vec::new();
        super::download_loop(&b"invalid\n[\"\",\"\",\"\",0]\n"[..], &mut out)?;
        let results: Vec<_> = out
            .split(|byte| *byte == b'\n')
            .filter(|line| !line.is_empty())
            .collect();
        assert_eq!(results.len(), 2);
        for result in results {
            let value: serde_json::Value = serde_json::from_slice(result)?;
            assert!(value.get("error").is_some());
        }
        let oversized = vec![b' '; (super::MAX_REQUEST_BYTES + 1) as usize];
        assert!(super::download_loop(oversized.as_slice(), &mut out).is_err());
        Ok(())
    }
    use super::{Error, download_cap, download_request, media_host, retry_media_response};
    use std::time::Duration;
    use ureq::http::Response;

    #[test]
    fn parallel_requests_require_distinct_size_gated_sources() {
        use serde_json::json;
        let first = json!(["session", "a", "/left/source.jpg", 1, true]);
        let second = json!(["session", "b", "/right/source.jpg", 32 * 1024 * 1024, true]);
        let third = json!(["session", "c", "/third/source.jpg", 1, true]);
        let fourth = json!(["session", "d", "/fourth/source.jpg", 1, true]);
        assert!(super::parallel_requests(json!([first, second])).is_ok());
        assert!(super::parallel_requests(json!([first, second, third, fourth])).is_ok());
        let eight: Vec<_> = (0..8)
            .map(|i| {
                json!([
                    "session",
                    i.to_string(),
                    format!("/stage-{i}/source.jpg"),
                    1,
                    true
                ])
            })
            .collect();
        assert!(super::parallel_requests(json!(eight)).is_ok());
        let nine: Vec<_> = (0..9)
            .map(|i| {
                json!([
                    "session",
                    i.to_string(),
                    format!("/stage-{i}/source.jpg"),
                    1,
                    true
                ])
            })
            .collect();
        assert!(super::parallel_requests(json!(nine)).is_err());
        let mut wide = second.clone();
        wide[3] = json!(super::MAX_PAIRED_VIDEO_BYTES);
        assert!(super::parallel_requests(json!([first, wide])).is_ok());
        assert!(super::parallel_requests(json!([first, wide, third])).is_ok());
        assert!(super::parallel_requests(json!([first, wide, third, fourth])).is_ok());
        let mut wider = eight.clone();
        wider[0][3] = json!(super::MAX_PAIRED_VIDEO_BYTES);
        assert!(super::parallel_requests(json!(&wider[..6])).is_ok());
        assert!(super::parallel_requests(json!(&wider[..7])).is_err());
        assert!(super::parallel_requests(json!([first])).is_err());
        assert!(super::parallel_requests(json!([first, second, second])).is_err());
        assert!(super::parallel_requests(json!([first, second, third, fourth, fourth])).is_err());
        for (index, replacement) in [
            (0, json!("different session")),
            (1, json!("a")),
            (2, json!("/left/source.jpg")),
            (3, json!(0)),
            (3, json!(super::MAX_PAIRED_VIDEO_BYTES + 1)),
            (4, json!(false)),
        ] {
            let mut invalid = second.clone();
            invalid[index] = replacement;
            assert!(super::parallel_requests(json!([first, invalid])).is_err());
        }
    }

    #[test]
    fn temporary_media_errors_retry_with_bounded_delays() -> Result<(), Error> {
        let mut replies = [
            Response::builder().status(500).body(())?,
            Response::builder()
                .status(503)
                .header("Retry-After", "5")
                .body(())?,
            Response::builder().status(200).body(())?,
        ]
        .into_iter();
        let mut retries = 0;
        let mut waited = Vec::new();
        let response = retry_media_response(
            &mut retries,
            || {
                replies
                    .next()
                    .ok_or_else(|| "Unexpected extra retry".into())
            },
            |delay| waited.push(delay),
        )?;
        assert_eq!(response.status().as_u16(), 200);
        assert_eq!(retries, 2);
        assert_eq!(waited, [Duration::from_secs(1), Duration::from_secs(5)]);
        let exhausted = retry_media_response(
            &mut retries,
            || Ok(Response::builder().status(504).body(())?),
            |delay| waited.push(delay),
        )?;
        assert_eq!(exhausted.status().as_u16(), 504);
        assert_eq!(waited.len(), 2);
        Ok(())
    }

    #[test]
    fn permanent_or_long_delayed_errors_do_not_retry() -> Result<(), Error> {
        for (status, retry_after) in [
            (403, ""),
            (404, ""),
            (429, "1"),
            (503, "31"),
            (503, "later"),
        ] {
            let mut retries = 0;
            let mut calls = 0;
            let response = retry_media_response(
                &mut retries,
                || {
                    calls += 1;
                    Ok(Response::builder()
                        .status(status)
                        .header("Retry-After", retry_after)
                        .body(())?)
                },
                |_| {},
            )?;
            assert_eq!(response.status().as_u16(), status);
            assert_eq!((calls, retries), (1, 0));
        }
        Ok(())
    }
    #[test]
    fn optional_size_requirement_preserves_old_requests_and_rejects_invalid_flags()
    -> Result<(), Error> {
        assert!(!download_request(br#"["batch","item","file",10]"#)?.4);
        assert!(download_request(br#"["batch","item","file",10,true]"#)?.4);
        assert!(!download_request(br#"["batch","item","file",10,false]"#)?.4);
        assert!(download_request(br#"["batch","item","file",10,"true"]"#).is_err());
        assert!(download_request(br#"["batch","item","file",10,true,false]"#).is_err());
        Ok(())
    }
    #[test]
    fn download_allowance_supports_ui_limits_and_rejects_hostile_values() -> Result<(), Error> {
        for gib in [2_u64, 4, 8, 16, 32] {
            let bytes = gib * 1024 * 1024 * 1024;
            assert_eq!(download_cap(&bytes.to_string())?, bytes);
            assert_eq!(
                download_cap(&(bytes - 256 * 1024 * 1024).to_string())?,
                bytes - 256 * 1024 * 1024
            );
        }
        for bad in [
            "0",
            "-1",
            "NaN",
            "true",
            "34359738369",
            "18446744073709551616",
            "",
            "8 GiB",
        ] {
            assert!(download_cap(bad).is_err());
        }
        Ok(())
    }
    #[test]
    fn credentials_and_redirects_remain_on_google_media_hosts() {
        assert!(media_host("https://lh3.googleusercontent.com/path", false));
        assert!(media_host(
            "https://rr1---sn.example.googlevideo.com/video",
            true
        ));
        assert!(!media_host("https://rr1.googlevideo.com/video", false));
        for value in [
            "http://lh3.googleusercontent.com/x",
            "https://googleusercontent.com.evil.test/x",
            "https://evil.test@lh3.googleusercontent.com/x",
            "https://lh3.googleusercontent.com:8443/x",
            "https://127.0.0.1/x",
        ] {
            assert!(!media_host(value, true));
        }
    }
}
