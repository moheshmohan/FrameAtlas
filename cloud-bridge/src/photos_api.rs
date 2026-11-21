//! Small Picker client on FilmCraft's existing OAuth vault. No editor/codec dependency.
//! Google Photos Picker REST v1: developers.google.com/photos/picker/reference/rest
use super::Error;
use filmcraft_oauth::{OAuthClient, Provider, TokenVault};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    env, fs,
    io::Read,
    path::PathBuf,
    time::Duration,
};

const API: &str = "https://photospicker.googleapis.com/v1";
const SCOPE: &str = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly";
const MAX_JSON: u64 = 4 * 1024 * 1024;
#[derive(PartialEq)]
pub enum MediaKind {
    Photo,
    Video,
    Unknown,
}
impl MediaKind {
    pub fn as_str(&self) -> &str {
        match self {
            Self::Photo => "photo",
            Self::Video => "video",
            Self::Unknown => "unknown",
        }
    }
}
pub struct Session {
    pub id: String,
    pub picker_uri: String,
    pub expire_time: Option<String>,
    pub media_items_set: bool,
}
pub struct Item {
    pub id: String,
    pub filename: String,
    pub kind: MediaKind,
    pub created: String,
    pub mime_type: String,
    pub base_url: String,
    pub video_status: Option<String>,
    /// What Google reports about the capture: camera make and model, picture size, frame rate.
    /// Google strips the position from its downloads and does not report it here either.
    pub capture: Value,
}
pub struct GooglePhotos {
    token: String,
    http: ureq::Agent,
}
fn text(v: &Value, key: &str) -> String {
    v.get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .chars()
        .take(16384)
        .collect()
}
fn session(v: Value) -> Result<Session, Error> {
    let id = text(&v, "id");
    if id.is_empty() || id.len() > 1024 {
        return Err("Google returned an invalid Picker session".into());
    }
    let uri = text(&v, "pickerUri");
    if !uri.is_empty() && !uri.starts_with("https://photos.google.com/") {
        return Err("Invalid Picker host".into());
    }
    Ok(Session {
        id,
        picker_uri: uri,
        expire_time: v
            .get("expireTime")
            .and_then(Value::as_str)
            .map(str::to_string),
        media_items_set: v
            .get("mediaItemsSet")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    })
}
impl GooglePhotos {
    pub fn configured() -> Result<Self, Error> {
        let dir = match env::var_os("FILMCRAFT_PHOTOS_DIR") {
            Some(d) => PathBuf::from(d),
            None => PathBuf::from(env::var_os("HOME").ok_or("Home directory unavailable")?)
                .join("Library/Application Support/FilmCraft/google-photos"),
        };
        let vault = TokenVault::system(&dir, "FilmCraft Google Photos");
        let (mut tokens, _) = vault
            .load()?
            .ok_or("Google Photos is not signed in; run filmcraft-cli photos auth")?;
        if !tokens.grants(SCOPE) {
            return Err("Google Picker consent missing; run filmcraft-cli photos auth".into());
        }
        let http: ureq::Agent = ureq::Agent::config_builder()
            .tls_config(
                ureq::tls::TlsConfig::builder()
                    .provider(ureq::tls::TlsProvider::Rustls)
                    .root_certs(ureq::tls::RootCerts::PlatformVerifier)
                    .unversioned_rustls_crypto_provider(std::sync::Arc::new(
                        rustls_rustcrypto::provider(),
                    ))
                    .build(),
            )
            .https_only(true)
            .http_status_as_error(false)
            .max_redirects(0)
            .timeout_connect(Some(Duration::from_secs(30)))
            .timeout_global(Some(Duration::from_secs(120)))
            .build()
            .into();
        if tokens.needs_refresh() {
            // Configuration is read only inside this private native broker. Tokens are loaded
            // and saved exclusively through FilmCraft's existing secure vault API.
            let path = dir.join("client_secret.json");
            if fs::metadata(&path)?.len() > 65536 {
                return Err("Native OAuth client configuration is too large".into());
            }
            let cfg: Value = serde_json::from_slice(&fs::read(&path)?)
                .map_err(|_| "Native OAuth client configuration is invalid")?;
            let cfg = cfg
                .get("installed")
                .or_else(|| cfg.get("web"))
                .ok_or("Native OAuth Desktop client is missing")?;
            let client = OAuthClient {
                provider: Provider::google(),
                client_id: text(cfg, "client_id"),
                client_secret: Some(text(cfg, "client_secret")),
                sign_in_command: "filmcraft-cli photos auth".into(),
            };
            tokens = client.refresh(&http, &tokens)?;
            vault.save(&tokens)?;
        }
        Ok(Self {
            token: tokens.access_token,
            http,
        })
    }
    pub fn agent(&self) -> ureq::Agent {
        self.http.clone()
    }
    pub fn access_token(&self) -> Result<String, Error> {
        Ok(self.token.clone())
    }
    pub fn same_access_token(&self, other: &Self) -> bool {
        self.token == other.token
    }
    fn call(&self, method: &str, endpoint: &str, body: Option<Value>) -> Result<Value, Error> {
        let url = format!("{API}{endpoint}");
        let auth = format!("Bearer {}", self.token);
        let result = match method {
            "POST" => self
                .http
                .post(&url)
                .header("Authorization", &auth)
                .send_json(body.unwrap_or(json!({}))),
            "DELETE" => self.http.delete(&url).header("Authorization", &auth).call(),
            _ => self.http.get(&url).header("Authorization", &auth).call(),
        }
        .map_err(|_| "Google Picker request failed; check connection and sign-in")?;
        let status = result.status().as_u16();
        if !(200..300).contains(&status) {
            return Err(format!(
                "Google Picker returned {status}; refresh sign-in or select the media again"
            )
            .into());
        }
        if method == "DELETE" {
            return Ok(json!({}));
        }
        let mut bytes = Vec::new();
        result
            .into_body()
            .into_reader()
            .take(MAX_JSON + 1)
            .read_to_end(&mut bytes)?;
        if bytes.len() as u64 > MAX_JSON {
            return Err("Google response exceeded its cap".into());
        }
        serde_json::from_slice(&bytes).map_err(|_| "Google returned invalid JSON".into())
    }
    pub fn create_session(&self, max: Option<u32>) -> Result<Session, Error> {
        session(self.call("POST", "/sessions", Some(json!({"pickingConfig":{"maxItemCount":max.unwrap_or(2000).clamp(1,2000).to_string()}})))?)
    }
    pub fn session(&self, id: &str) -> Result<Session, Error> {
        session(self.call(
            "GET",
            &format!("/sessions/{}", filmcraft_oauth::urlencode(id)),
            None,
        )?)
    }
    pub fn delete_session(&self, id: &str) -> Result<(), Error> {
        self.call(
            "DELETE",
            &format!("/sessions/{}", filmcraft_oauth::urlencode(id)),
            None,
        )
        .map(drop)
    }
    pub fn picked_items(&self, id: &str) -> Result<Vec<Item>, Error> {
        self.read_items(id, None)
    }
    pub fn picked_item(&self, id: &str, wanted: &str) -> Result<Option<Item>, Error> {
        if wanted.is_empty() || wanted.len() > 1024 {
            return Err("Invalid selected item identity".into());
        }
        Ok(self.read_items(id, Some(wanted))?.into_iter().next())
    }
    fn read_items(&self, id: &str, wanted: Option<&str>) -> Result<Vec<Item>, Error> {
        read_pages(wanted, |next| self.selection_page(id, next))
    }
    pub fn selection_page(&self, id: &str, next: &str) -> Result<Value, Error> {
        self.call(
            "GET",
            &format!(
                "/mediaItems?sessionId={}&pageSize=100&pageToken={}",
                filmcraft_oauth::urlencode(id),
                filmcraft_oauth::urlencode(next)
            ),
            None,
        )
    }
}

fn selected_item(v: &Value) -> Result<Item, Error> {
    let file = v
        .get("mediaFile")
        .ok_or("Selected item has no media file")?;
    let id = text(v, "id");
    let filename = text(file, "filename");
    if id.is_empty() || id.len() > 1024 || filename.is_empty() || filename.len() > 2048 {
        return Err("Invalid selected item".into());
    }
    let kind = match text(v, "type").as_str() {
        "VIDEO" => MediaKind::Video,
        "PHOTO" => MediaKind::Photo,
        _ => MediaKind::Unknown,
    };
    Ok(Item {
        id,
        filename,
        kind,
        created: text(v, "createTime"),
        mime_type: text(file, "mimeType"),
        base_url: text(file, "baseUrl"),
        video_status: file
            .pointer("/mediaFileMetadata/videoMetadata/processingStatus")
            .and_then(Value::as_str)
            .map(str::to_string),
        capture: capture_metadata(file),
    })
}

/// The camera fields of a Picker `mediaFileMetadata`, bounded: text trimmed to 120 characters,
/// numbers kept only when finite, positive and plausible.
fn capture_metadata(file: &Value) -> Value {
    let Some(meta) = file.get("mediaFileMetadata") else {
        return json!({});
    };
    let mut out = serde_json::Map::new();
    for (from, to) in [("cameraMake", "make"), ("cameraModel", "model")] {
        let value: String = text(meta, from).trim().chars().take(120).collect();
        if !value.is_empty() {
            out.insert(to.into(), Value::String(value));
        }
    }
    let number = |v: Option<&Value>, max: f64| {
        v.and_then(Value::as_f64)
            .filter(|n| n.is_finite() && *n > 0.0 && *n <= max)
    };
    for (key, max) in [("width", 200_000.0), ("height", 200_000.0)] {
        if let Some(n) = number(meta.get(key), max) {
            out.insert(key.into(), json!(n.round() as u64));
        }
    }
    if let Some(fps) = number(meta.pointer("/videoMetadata/fps"), 2_000.0) {
        out.insert("fps".into(), json!(fps));
    }
    if let Some(photo) = meta.get("photoMetadata") {
        if let Some(n) = number(photo.get("focalLength"), 10_000.0) {
            out.insert("focal_length_mm".into(), json!(n));
        }
        if let Some(n) = number(photo.get("apertureFNumber"), 1_000.0) {
            out.insert("aperture_f".into(), json!(n));
        }
        if let Some(n) = number(photo.get("isoEquivalent"), 10_000_000.0) {
            out.insert("iso".into(), json!(n.round() as u64));
        }
        let exposure: String = text(photo, "exposureTime").chars().take(32).collect();
        if !exposure.is_empty() {
            out.insert("exposure".into(), Value::String(exposure));
        }
    }
    Value::Object(out)
}

struct PageCursor {
    next: Option<String>,
    tokens: HashSet<String>,
    identities: HashSet<String>,
}

impl PageCursor {
    fn new() -> Self {
        Self {
            next: Some(String::new()),
            tokens: HashSet::new(),
            identities: HashSet::new(),
        }
    }

    fn read(
        &mut self,
        fetch: impl FnOnce(&str) -> Result<Value, Error>,
    ) -> Result<Option<Vec<Item>>, Error> {
        let Some(next) = self.next.as_deref() else {
            return Ok(None);
        };
        if self.tokens.len() >= 128 {
            return Err("Google selection has too many pages; choose a smaller batch instead of importing partial metadata".into());
        }
        if self.tokens.contains(next) {
            return Err("Google repeated a selection page token; import was not completed".into());
        }
        let page = fetch(next)?;
        if !page.is_object() {
            return Err("Google returned an invalid selection page".into());
        }
        let values = match page.get("mediaItems") {
            None => &[][..],
            Some(Value::Array(values)) if values.len() <= 100 => values.as_slice(),
            _ => return Err("Google returned an invalid or oversized selection page".into()),
        };
        let next_token = match page.get("nextPageToken") {
            None | Some(Value::Null) => None,
            Some(Value::String(token)) if token.is_empty() => None,
            Some(Value::String(token)) if token.len() <= 16384 => Some(token.clone()),
            _ => return Err("Google returned an invalid selection page token".into()),
        };
        let mut items = Vec::with_capacity(values.len());
        let mut new_ids = HashSet::new();
        for value in values {
            let item = selected_item(value)?;
            if self.identities.contains(&item.id) || !new_ids.insert(item.id.clone()) {
                return Err("Google repeated an item identity; selection was not completed".into());
            }
            items.push(item);
        }
        if self.identities.len() + new_ids.len() > 2000 {
            return Err("Google selection exceeded 2,000 items; choose a smaller batch".into());
        }
        self.identities.extend(new_ids);
        self.tokens.insert(next.to_owned());
        self.next = next_token;
        Ok(Some(items))
    }
}

pub struct SelectionItems {
    cursor: PageCursor,
    items: HashMap<String, Item>,
}

impl SelectionItems {
    pub fn new() -> Self {
        Self {
            cursor: PageCursor::new(),
            items: HashMap::new(),
        }
    }

    pub fn get(&self, id: &str) -> Option<&Item> {
        self.items.get(id)
    }

    pub fn find(
        &mut self,
        wanted: &[&str],
        mut fetch: impl FnMut(&str) -> Result<Value, Error>,
    ) -> Result<usize, Error> {
        let mut pages = 0;
        while wanted.iter().any(|id| !self.items.contains_key(*id)) {
            let Some(items) = self.cursor.read(&mut fetch)? else {
                break;
            };
            self.items
                .extend(items.into_iter().map(|item| (item.id.clone(), item)));
            pages += 1;
        }
        Ok(pages)
    }
}

// Full inventory follows short and empty pages to the real end.
fn read_pages<F>(wanted: Option<&str>, mut fetch: F) -> Result<Vec<Item>, Error>
where
    F: FnMut(&str) -> Result<Value, Error>,
{
    let mut cursor = PageCursor::new();
    let mut out = Vec::new();
    while let Some(items) = cursor.read(&mut fetch)? {
        if let Some(id) = wanted {
            if let Some(item) = items.into_iter().find(|item| item.id == id) {
                return Ok(vec![item]);
            }
        } else {
            out.extend(items);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::{Error, SelectionItems, read_pages, selected_item};
    use serde_json::{Value, json};

    fn item(id: &str) -> Value {
        json!({"id":id,"type":"PHOTO","createTime":"2025-12-01T12:00:00Z",
               "mediaFile":{"filename":format!("{id}.jpg"),"baseUrl":"https://lh3.googleusercontent.com/fixture","mimeType":"image/jpeg"}})
    }

    #[test]
    fn selected_item_keeps_bounded_camera_metadata() -> Result<(), Error> {
        let v = json!({"id":"p","type":"PHOTO","createTime":"2024-07-01T18:30:00Z",
            "mediaFile":{"filename":"IMG_1.HEIC","baseUrl":"https://lh3.googleusercontent.com/x","mimeType":"image/heic",
              "mediaFileMetadata":{"width":4032,"height":3024,"cameraMake":"Apple","cameraModel":"iPhone 15 Pro",
                "photoMetadata":{"focalLength":6.86,"apertureFNumber":1.78,"isoEquivalent":50,"exposureTime":"0.008s"}}}});
        let item = selected_item(&v)?;
        assert_eq!(item.capture, json!({"make":"Apple","model":"iPhone 15 Pro","width":4032,"height":3024,
            "focal_length_mm":6.86,"aperture_f":1.78,"iso":50,"exposure":"0.008s"}));
        let hostile = json!({"id":"v","type":"VIDEO","mediaFile":{"filename":"v.mp4",
            "mediaFileMetadata":{"width":-1,"height":"tall","cameraMake":"  ","videoMetadata":{"fps":1e308}}}});
        assert_eq!(selected_item(&hostile)?.capture, json!({}));
        let bare = json!({"id":"b","type":"PHOTO","mediaFile":{"filename":"b.jpg"}});
        assert_eq!(selected_item(&bare)?.capture, json!({}));
        Ok(())
    }

    #[test]
    fn single_item_lookup_does_not_fetch_unneeded_later_pages() -> Result<(), Error> {
        let mut calls = 0;
        let result = read_pages(Some("wanted"), |_| {
            calls += 1;
            Ok(json!({"mediaItems":[item("wanted")],"nextPageToken":"unneeded"}))
        })?;
        assert_eq!(calls, 1);
        assert_eq!(result.first().map(|i| i.id.as_str()), Some("wanted"));
        Ok(())
    }

    #[test]
    fn incremental_lookup_resumes_at_saved_cursor_and_reuses_known_items() -> Result<(), Error> {
        let mut index = SelectionItems::new();
        assert_eq!(
            index.find(&["b", "a"], |token| {
                assert!(token.is_empty());
                Ok(json!({"mediaItems":[item("a"),item("b")],"nextPageToken":"next"}))
            })?,
            1
        );
        assert_eq!(
            index.find(&["a"], |_| Err("Known item should not fetch a page".into()))?,
            0
        );
        assert_eq!(
            index.find(&["a", "c"], |token| match token {
                "next" => Ok(json!({"nextPageToken":"last"})),
                "last" => Ok(json!({"mediaItems":[item("c")]})),
                _ => Err("Unexpected page token".into()),
            })?,
            2
        );
        assert_eq!(index.get("c").map(|item| item.id.as_str()), Some("c"));
        assert_eq!(
            index.find(&["missing"], |_| Err(
                "Exhausted selection should not fetch a page".into()
            ))?,
            0
        );
        assert!(index.get("missing").is_none());
        Ok(())
    }

    #[test]
    fn full_inventory_follows_empty_and_short_pages_beyond_old_limit() -> Result<(), Error> {
        let mut calls = 0;
        let result = read_pages(None, |_| {
            calls += 1;
            let values = if calls == 1 {
                vec![]
            } else {
                vec![item(&calls.to_string())]
            };
            Ok(if calls < 31 {
                json!({"mediaItems":values,"nextPageToken":calls.to_string()})
            } else {
                json!({"mediaItems":values})
            })
        })?;
        assert_eq!(calls, 31);
        assert_eq!(result.len(), 30);
        assert_eq!(result.last().map(|i| i.id.as_str()), Some("31"));
        Ok(())
    }

    #[test]
    fn missing_item_still_checks_the_whole_selection() -> Result<(), Error> {
        let mut calls = 0;
        let result = read_pages(Some("missing"), |_| {
            calls += 1;
            Ok(if calls == 1 {
                json!({"mediaItems":[item("one")],"nextPageToken":"second"})
            } else {
                json!({"mediaItems":[item("two")]})
            })
        })?;
        assert_eq!(calls, 2);
        assert!(result.is_empty());
        Ok(())
    }

    #[test]
    fn repeated_tokens_and_item_identities_fail_without_partial_results() {
        let mut calls = 0;
        let cycle = read_pages(None, |_| {
            calls += 1;
            Ok(json!({"mediaItems":[],"nextPageToken":"repeated"}))
        });
        assert!(cycle.is_err());
        assert_eq!(calls, 2);
        let mut page = 0;
        let duplicate = read_pages(None, |_| {
            page += 1;
            Ok(if page == 1 {
                json!({"mediaItems":[item("one")],"nextPageToken":"second"})
            } else {
                json!({"mediaItems":[item("one")]})
            })
        });
        assert!(duplicate.is_err());
    }

    #[test]
    fn hostile_page_shapes_counts_and_endless_pagination_are_bounded() {
        let oversized: Vec<_> = (0..101).map(|i| item(&i.to_string())).collect();
        assert!(read_pages(None, |_| Ok(json!({"mediaItems":oversized}))).is_err());
        assert!(read_pages(None, |_| Ok(json!({"mediaItems":"bad"}))).is_err());
        assert!(read_pages(None, |_| Ok(json!({"nextPageToken":42}))).is_err());
        assert!(read_pages(None, |_| Ok(json!([]))).is_err());
        assert!(read_pages(None, |_| Ok(json!({"mediaItems":[{"id":"missing-file"}]}))).is_err());
        let mut pages = 0;
        assert!(
            read_pages(None, |_| {
                pages += 1;
                Ok(json!({"nextPageToken":pages.to_string()}))
            })
            .is_err()
        );
        assert_eq!(pages, 128);
        let mut pages = 0;
        assert!(
            read_pages(None, |_| {
                pages += 1;
                let values: Vec<_> = (0..100).map(|i| item(&format!("{pages}-{i}"))).collect();
                Ok(json!({"mediaItems":values,"nextPageToken":pages.to_string()}))
            })
            .is_err()
        );
        assert_eq!(pages, 21);
    }
}
