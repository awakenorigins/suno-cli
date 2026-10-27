//! hCaptcha bypass via piloted Chrome.
//!
//! Suno gates `/api/generate/v2-web/` with an invisible hCaptcha challenge.
//! The request body's `token` field must contain a freshly-solved hCaptcha
//! response. Headless HTTP clients can't pass it; only a real browser with
//! a warm behavioural fingerprint does.
//!
//! This module spawns a hidden Chrome instance with `--remote-debugging-port`
//! enabled, injects the user's Suno cookies via CDP, navigates to
//! suno.com/create, then renders an invisible hCaptcha widget and calls
//! `hcaptcha.execute()` to obtain a token. The Chrome instance is reused
//! across calls so subsequent generations are fast.
//!
//! Discovered + verified end-to-end on 2026-04-08.

use std::collections::HashSet;
use std::process::Stdio;
use std::sync::OnceLock;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::Mutex;
use tokio::time::{sleep, timeout};
use tokio_tungstenite::tungstenite::Message;

use crate::auth::AuthState;
use crate::errors::CliError;

/// Suno's hCaptcha sitekey, captured from the live web app's
/// `hcaptcha.render(...)` arguments on 2026-04-08.
const SUNO_HCAPTCHA_SITEKEY: &str = "d65453de-3f1a-4aac-9366-a0f06e52b2ce";
/// Port for the suno-cli managed Chrome instance. Picked high to avoid
/// colliding with the user's main Chrome (which is rarely on 9233).
const CDP_PORT: u16 = 9233;
const CDP_HOST: &str = "127.0.0.1";

/// Singleton holder for the Chrome process so the same instance survives
/// across multiple `generate()` calls within one CLI invocation.
static CHROME: OnceLock<Mutex<Option<Child>>> = OnceLock::new();

fn chrome_slot() -> &'static Mutex<Option<Child>> {
    CHROME.get_or_init(|| Mutex::new(None))
}

/// Solve a fresh hCaptcha challenge and return the token to attach to a
/// `/api/generate/v2-web/` request body.
pub async fn solve(auth: &AuthState) -> Result<String, CliError> {
    ensure_chrome_running().await?;
    let target = find_or_create_suno_tab().await?;
    let token = render_and_execute(&target.web_socket_debugger_url, auth).await?;
    Ok(token)
}

/// Either reuse a Chrome instance already listening on `CDP_PORT` or spawn
/// a new hidden one with the suno-cli profile dir. Idempotent.
async fn ensure_chrome_running() -> Result<(), CliError> {
    if cdp_version().await.is_ok() {
        return Ok(());
    }

    // Need to spawn it.
    let chrome_path = locate_chrome()?;
    let profile_dir = directories::ProjectDirs::from("com", "suno-cli", "suno-cli")
        .map(|d| d.data_dir().join("chrome-profile"))
        .ok_or_else(|| CliError::Config("could not resolve data dir for chrome profile".into()))?;
    std::fs::create_dir_all(&profile_dir)?;

    eprintln!("Launching headless Chrome for captcha solver (one-time per session)...");

    // NOTE: do NOT use --headless. hCaptcha's bot-detection trips on headless
    // mode and returns "challenge-expired". We run a real headed Chrome but
    // shove it far offscreen. Keep a desktop-sized viewport: a 1x1 window makes
    // Suno serve its mobile/app interstitial, which never loads hCaptcha.
    let mut child = Command::new(&chrome_path)
        .arg(format!("--remote-debugging-port={CDP_PORT}"))
        .arg(format!("--user-data-dir={}", profile_dir.display()))
        .arg("--no-first-run")
        .arg("--no-default-browser-check")
        .arg("--disable-search-engine-choice-screen")
        .arg("--disable-features=TranslateUI")
        .arg("--window-position=-32000,-32000")
        .arg("--window-size=1280,900")
        .arg("--silent-launch")
        .arg("about:blank")
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| CliError::Config(format!("failed to spawn Chrome at {chrome_path:?}: {e}")))?;
    drain_stderr(&mut child);

    {
        let mut slot = chrome_slot().lock().await;
        *slot = Some(child);
    }

    // Wait up to 10s for CDP to come up
    for _ in 0..20 {
        sleep(Duration::from_millis(500)).await;
        if cdp_version().await.is_ok() {
            return Ok(());
        }
    }

    Err(CliError::Config(
        "Chrome was spawned but never opened the CDP port. Check that Chrome can start normally, or set SUNO_CHROME_PATH to a Chrome/Chromium binary.".into(),
    ))
}

/// Locate a Chrome binary on the host. Looks in the usual macOS / Linux /
/// Windows install paths and falls back to `$PATH`.
fn locate_chrome() -> Result<String, CliError> {
    if let Ok(path) = std::env::var("SUNO_CHROME_PATH")
        && !path.trim().is_empty()
    {
        if std::path::Path::new(&path).exists() {
            return Ok(path);
        }
        return Err(CliError::Config(format!(
            "SUNO_CHROME_PATH points to a missing file: {path}"
        )));
    }

    let candidates: &[&str] = if cfg!(target_os = "macos") {
        &[
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    } else if cfg!(target_os = "linux") {
        &[
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]
    } else {
        &[
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        ]
    };
    for c in candidates {
        if std::path::Path::new(c).exists() {
            return Ok(c.to_string());
        }
    }
    Err(CliError::Config(
        "Could not find a Chrome/Chromium binary. Install Google Chrome or set SUNO_CHROME_PATH."
            .into(),
    ))
}

/// CDP target descriptor returned by `/json/list`.
#[derive(Debug, Deserialize)]
struct Target {
    #[serde(rename = "type")]
    target_type: String,
    url: String,
    #[serde(rename = "webSocketDebuggerUrl")]
    web_socket_debugger_url: String,
}

async fn cdp_version() -> Result<serde_json::Value, CliError> {
    let url = format!("http://{CDP_HOST}:{CDP_PORT}/json/version");
    let resp = reqwest::Client::new()
        .get(&url)
        .timeout(Duration::from_secs(2))
        .send()
        .await
        .map_err(|e| CliError::Config(format!("CDP /json/version: {e}")))?;
    let v: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| CliError::Config(format!("CDP json parse: {e}")))?;
    Ok(v)
}

async fn cdp_list() -> Result<Vec<Target>, CliError> {
    let url = format!("http://{CDP_HOST}:{CDP_PORT}/json/list");
    let resp = reqwest::Client::new()
        .get(&url)
        .timeout(Duration::from_secs(5))
        .send()
        .await
        .map_err(|e| CliError::Config(format!("CDP /json/list: {e}")))?;
    let list: Vec<Target> = resp
        .json()
        .await
        .map_err(|e| CliError::Config(format!("CDP json parse: {e}")))?;
    Ok(list)
}

/// Find the existing page in the managed Chrome, or open a new blank one.
/// Navigation to suno.com/create happens only after we have cleared stale
/// cookies and installed the small cookie subset the captcha page needs.
async fn find_or_create_suno_tab() -> Result<Target, CliError> {
    let targets = cdp_list().await?;
    if let Some(t) = targets.into_iter().find(|t| {
        t.target_type == "page"
            && !t.web_socket_debugger_url.is_empty()
            && !t.url.starts_with("chrome://")
    }) {
        return Ok(t);
    }

    // No page target — open a blank one. CDP exposes a /json/new?url= helper.
    let url = format!(
        "http://{CDP_HOST}:{CDP_PORT}/json/new?{}",
        urlencode("about:blank")
    );
    let resp = reqwest::Client::new()
        .put(&url)
        .timeout(Duration::from_secs(10))
        .send()
        .await
        .map_err(|e| CliError::Config(format!("CDP /json/new: {e}")))?;
    let t: Target = resp
        .json()
        .await
        .map_err(|e| CliError::Config(format!("CDP /json/new parse: {e}")))?;
    // Give the page a moment to start loading before we attach
    sleep(Duration::from_millis(800)).await;
    Ok(t)
}

fn urlencode(s: &str) -> String {
    s.replace(":", "%3A").replace("/", "%2F")
}

/// CDP request envelope.
#[derive(Serialize)]
struct CdpReq<'a> {
    id: u64,
    method: &'a str,
    params: serde_json::Value,
}

/// CDP cookie struct (subset of Network.CookieParam).
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CdpCookie {
    name: String,
    value: String,
    domain: String,
    path: String,
    secure: bool,
    http_only: bool,
    same_site: &'static str,
}

type CdpStream =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// Send a CDP method and wait for the response (drains intervening events).
async fn cdp_call(
    ws: &mut CdpStream,
    id: u64,
    method: &str,
    params: serde_json::Value,
) -> Result<serde_json::Value, CliError> {
    let req = CdpReq { id, method, params };
    let payload = serde_json::to_string(&req).unwrap();
    ws.send(Message::Text(payload))
        .await
        .map_err(|e| CliError::Config(format!("CDP ws send {method}: {e}")))?;
    loop {
        let msg = timeout(Duration::from_secs(60), ws.next())
            .await
            .map_err(|_| CliError::Config(format!("CDP {method} timeout")))?
            .ok_or_else(|| CliError::Config(format!("CDP {method} ws closed")))?
            .map_err(|e| CliError::Config(format!("CDP {method} ws err: {e}")))?;
        let text = match msg {
            Message::Text(t) => t.to_string(),
            Message::Binary(_) | Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => {
                continue;
            }
            Message::Close(_) => {
                return Err(CliError::Config(format!("CDP {method} ws closed mid-call")));
            }
        };
        let v: serde_json::Value = serde_json::from_str(&text)
            .map_err(|e| CliError::Config(format!("CDP {method} json: {e}")))?;
        if v.get("id").and_then(|x| x.as_u64()) == Some(id) {
            if let Some(err) = v.get("error") {
                return Err(CliError::Config(format!("CDP {method} error: {err}")));
            }
            return Ok(v.get("result").cloned().unwrap_or(serde_json::Value::Null));
        }
        // Ignore unrelated events
    }
}

/// Connect to the page websocket, inject cookies, navigate to suno.com/create
/// if needed, then render an invisible hCaptcha widget and call
/// `hcaptcha.execute()` to obtain a token.
async fn render_and_execute(ws_url: &str, auth: &AuthState) -> Result<String, CliError> {
    let (mut ws, _) = tokio_tungstenite::connect_async(ws_url)
        .await
        .map_err(|e| CliError::Config(format!("CDP ws connect: {e}")))?;

    let mut next_id: u64 = 0;
    let mut next = || -> u64 {
        next_id += 1;
        next_id
    };

    cdp_call(&mut ws, next(), "Network.enable", serde_json::json!({})).await?;
    cdp_call(&mut ws, next(), "Page.enable", serde_json::json!({})).await?;
    cdp_call(&mut ws, next(), "Runtime.enable", serde_json::json!({})).await?;
    cdp_call(
        &mut ws,
        next(),
        "Emulation.setDeviceMetricsOverride",
        serde_json::json!({
            "width": 1280,
            "height": 900,
            "deviceScaleFactor": 1,
            "mobile": false
        }),
    )
    .await?;

    // The managed profile is persistent across CLI invocations. Clear any
    // previously injected full browser cookie set first; stale analytics and
    // Clerk duplicates can make suno.com/create fail with HTTP 431 before
    // hCaptcha even loads.
    cdp_call(
        &mut ws,
        next(),
        "Network.clearBrowserCookies",
        serde_json::json!({}),
    )
    .await?;

    // Inject only the narrow Suno/Clerk cookie subset required to let the web
    // app initialize. Replaying the whole browser Cookie header is brittle and
    // can exceed Suno/Clerk request-header limits.
    let cookies = extract_cookies(auth)?;
    if !cookies.is_empty() {
        cdp_call(
            &mut ws,
            next(),
            "Network.setCookies",
            serde_json::json!({ "cookies": cookies }),
        )
        .await?;
    }

    cdp_call(
        &mut ws,
        next(),
        "Page.navigate",
        serde_json::json!({ "url": "https://suno.com/create" }),
    )
    .await?;

    // Poll for hcaptcha global (up to 30s).
    let mut ready = false;
    for _ in 0..30 {
        sleep(Duration::from_secs(1)).await;
        let probe = cdp_call(
            &mut ws,
            next(),
            "Runtime.evaluate",
            serde_json::json!({
                "expression": "typeof hcaptcha !== 'undefined' && !!hcaptcha.render",
                "returnByValue": true,
            }),
        )
        .await?;
        if probe
            .get("result")
            .and_then(|r| r.get("value"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
        {
            ready = true;
            break;
        }
    }
    if !ready {
        let page_state = page_state_excerpt(&mut ws, &mut next).await?;
        return Err(CliError::Config(format!(
            "hcaptcha never finished loading on suno.com/create ({page_state})"
        )));
    }
    // Extra settle so the SDK is fully wired up.
    sleep(Duration::from_secs(2)).await;

    // Render an invisible widget and execute it
    let solve_js = format!(
        r#"
        (async () => {{
            try {{
                const div = document.createElement('div');
                div.style.cssText = 'position:fixed;top:-9999px;left:-9999px;';
                document.body.appendChild(div);
                const id = hcaptcha.render(div, {{
                    sitekey: '{SUNO_HCAPTCHA_SITEKEY}',
                    size: 'invisible',
                    sentry: false,
                    endpoint: 'https://hcaptcha-endpoint-prod.suno.com',
                    assethost: 'https://hcaptcha-assets-prod.suno.com',
                    imghost: 'https://hcaptcha-imgs-prod.suno.com',
                    reportapi: 'https://hcaptcha-reportapi-prod.suno.com',
                }});
                const r = await hcaptcha.execute(id, {{ async: true }});
                return (r && r.response) ? r.response : '';
            }} catch (e) {{
                return 'ERR:' + String(e);
            }}
        }})()
        "#
    );

    let result = cdp_call(
        &mut ws,
        next(),
        "Runtime.evaluate",
        serde_json::json!({
            "expression": solve_js,
            "awaitPromise": true,
            "returnByValue": true,
        }),
    )
    .await?;

    let token = result
        .get("result")
        .and_then(|r| r.get("value"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    if token.is_empty() {
        return Err(CliError::Config("hcaptcha returned empty token".into()));
    }
    if token.starts_with("ERR:") {
        return Err(CliError::Config(format!("hcaptcha solver: {token}")));
    }
    Ok(token)
}

/// Pull the user's Suno cookies from their main Chrome via `rookie`.
/// Returns them in CDP `Network.CookieParam` shape.
async fn page_state_excerpt(
    ws: &mut CdpStream,
    next: &mut impl FnMut() -> u64,
) -> Result<String, CliError> {
    let state = cdp_call(
        ws,
        next(),
        "Runtime.evaluate",
        serde_json::json!({
            "expression": "JSON.stringify({ href: location.href, body: (document.body && document.body.innerText || '').slice(0, 240) })",
            "returnByValue": true,
        }),
    )
    .await?;
    let raw = state
        .get("result")
        .and_then(|r| r.get("value"))
        .and_then(|v| v.as_str())
        .unwrap_or("{}");
    let parsed: serde_json::Value = serde_json::from_str(raw).unwrap_or_default();
    let href = parsed.get("href").and_then(|v| v.as_str()).unwrap_or("");
    let body = parsed
        .get("body")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .replace(['\n', '\r'], " ");
    if body.is_empty() {
        Ok(format!("page={href}"))
    } else {
        Ok(format!("page={href}; body={body}"))
    }
}

fn extract_cookies(auth: &AuthState) -> Result<Vec<CdpCookie>, CliError> {
    let mut out = Vec::new();
    let mut seen = HashSet::new();

    // Prefer persisted CLI auth cookies first. Live browser cookies may come from a
    // logged-out/guest profile and can poison the captcha page; only use them as fallback.

    if let Some(clerk) = auth
        .clerk_client_cookie
        .as_deref()
        .filter(|c| !c.trim().is_empty())
    {
        push_cookie(
            &mut out,
            &mut seen,
            "__client",
            clerk.trim(),
            "auth.suno.com",
            true,
        );
        push_cookie(
            &mut out,
            &mut seen,
            "__client",
            clerk.trim(),
            ".suno.com",
            true,
        );
    }

    if let Some(session_id) = auth.session_id.as_deref().filter(|s| !s.trim().is_empty()) {
        push_cookie(
            &mut out,
            &mut seen,
            "__session",
            session_id.trim(),
            ".suno.com",
            true,
        );
        push_cookie(
            &mut out,
            &mut seen,
            "__session",
            session_id.trim(),
            "suno.com",
            true,
        );
        push_cookie(
            &mut out,
            &mut seen,
            "__session",
            session_id.trim(),
            "auth.suno.com",
            true,
        );
    }

    if let Some(device_id) = auth.device_id.as_deref().filter(|d| !d.trim().is_empty()) {
        push_cookie(
            &mut out,
            &mut seen,
            "ajs_anonymous_id",
            device_id.trim(),
            ".suno.com",
            false,
        );
        push_cookie(
            &mut out,
            &mut seen,
            "suno_device_id",
            device_id.trim(),
            ".suno.com",
            false,
        );
    }

    if let Some(cookie_header) = auth.cookie.as_deref().filter(|c| !c.trim().is_empty()) {
        add_minimal_cookies_from_header(cookie_header, &mut out, &mut seen);
    }

    if !out.is_empty() {
        return Ok(out);
    }

    if add_live_browser_cookies(&mut out, &mut seen) && !out.is_empty() {
        return Ok(out);
    }

    Ok(out)
}

fn add_live_browser_cookies(
    out: &mut Vec<CdpCookie>,
    seen: &mut HashSet<(String, String)>,
) -> bool {
    let domains: Vec<String> = vec![
        "suno.com".into(),
        "auth.suno.com".into(),
        ".suno.com".into(),
    ];
    let Some((browser_name, raw_cookies)) = [
        ("Chrome", rookie::chrome(Some(domains.clone()))),
        ("Arc", rookie::arc(Some(domains.clone()))),
        ("Brave", rookie::brave(Some(domains.clone()))),
        ("Firefox", rookie::firefox(Some(domains.clone()))),
        ("Edge", rookie::edge(Some(domains))),
    ]
    .into_iter()
    .find_map(|(browser_name, result)| match result {
        Ok(cookies) if cookies.iter().any(|c| c.domain.contains("suno.com")) => {
            Some((browser_name, cookies))
        }
        _ => None,
    }) else {
        return false;
    };

    for c in raw_cookies {
        if !c.domain.contains("suno.com") {
            continue;
        }
        add_minimal_cookie(&c.name, &c.value, &c.domain, c.http_only, out, seen);
    }

    if !out.is_empty() {
        eprintln!("Using fresh Suno browser cookies from {browser_name}");
    }
    !out.is_empty()
}

fn add_minimal_cookies_from_header(
    cookie_header: &str,
    out: &mut Vec<CdpCookie>,
    seen: &mut HashSet<(String, String)>,
) {
    for part in cookie_header.split(';') {
        let Some((name, value)) = part.trim().split_once('=') else {
            continue;
        };
        add_minimal_cookie(name.trim(), value.trim(), ".suno.com", false, out, seen);
    }
}

fn add_minimal_cookie(
    name: &str,
    value: &str,
    domain: &str,
    http_only: bool,
    out: &mut Vec<CdpCookie>,
    seen: &mut HashSet<(String, String)>,
) {
    if name.is_empty() || value.is_empty() || !is_captcha_cookie(name) {
        return;
    }

    if name == "__client" || name.starts_with("__client_") {
        push_cookie(out, seen, name, value, "auth.suno.com", true);
        push_cookie(out, seen, name, value, ".suno.com", true);
        return;
    }

    let cookie_domain = if domain.contains("auth.suno.com") {
        "auth.suno.com"
    } else {
        ".suno.com"
    };
    push_cookie(out, seen, name, value, cookie_domain, http_only);
}

fn is_captcha_cookie(name: &str) -> bool {
    matches!(
        name,
        "__session"
            | "clerk_active_context"
            | "ajs_anonymous_id"
            | "suno_device_id"
            | "statsig_stable_id"
            | "ssr_bucket"
            | "has_logged_in_before"
    ) || name == "__client"
        || name.starts_with("__client_")
        || name.starts_with("__session_")
}

fn push_cookie(
    out: &mut Vec<CdpCookie>,
    seen: &mut HashSet<(String, String)>,
    name: &str,
    value: &str,
    domain: &str,
    http_only: bool,
) {
    let key = (name.to_string(), domain.to_string());
    if !seen.insert(key) {
        return;
    }
    out.push(CdpCookie {
        name: name.to_string(),
        value: value.to_string(),
        domain: domain.to_string(),
        path: "/".to_string(),
        secure: true,
        http_only,
        same_site: "Lax",
    });
}

/// Drain the spawned Chrome's stderr in the background — keeps it from
/// blocking on a full pipe and gives us logs for debugging.
#[allow(dead_code)]
fn drain_stderr(child: &mut Child) {
    if let Some(stderr) = child.stderr.take() {
        let mut reader = BufReader::new(stderr).lines();
        tokio::spawn(async move {
            while let Ok(Some(_)) = reader.next_line().await {
                // discard
            }
        });
    }
}
