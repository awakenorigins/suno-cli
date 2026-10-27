pub mod billing;
pub mod concat;
pub mod cover;
pub mod delete;
pub mod feed;
pub mod generate;
pub mod lyrics;
pub mod metadata;
pub mod persona;
pub mod remaster;
pub mod stems;
pub mod types;

use std::sync::Mutex;

use reqwest::Client;

use crate::auth::{self, AuthState};
use crate::errors::CliError;

pub struct SunoClient {
    client: Client,
    /// Auth state behind a sync mutex so `&self` methods can transparently
    /// refresh the JWT mid-request when Suno returns
    /// `Token validation failed.` (their server-side staleness threshold
    /// kicks in well before the JWT's own `exp` claim). The lock is only
    /// held briefly to read/clone auth fields — never across awaits.
    auth: Mutex<AuthState>,
}

const BASE_URL: &str = "https://studio-api-prod.suno.com";

impl SunoClient {
    /// Create a new client. If JWT is expired but we have a Clerk cookie,
    /// auto-refresh the JWT transparently.
    pub async fn new_with_refresh(mut auth: AuthState) -> Result<Self, CliError> {
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .user_agent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
            .build()
            .map_err(|e| CliError::Config(format!("HTTP client: {e}")))?;

        if auth.is_jwt_expired() {
            // Try auto-refresh via Clerk cookie
            if let (Some(cookie), Some(session_id)) = (&auth.clerk_client_cookie, &auth.session_id)
            {
                eprintln!("JWT expired, refreshing via Clerk...");
                match auth::clerk_refresh_jwt(&client, cookie, session_id).await {
                    Ok(jwt) => {
                        auth.jwt = Some(jwt);
                        auth.save()?;
                        eprintln!("JWT refreshed successfully");
                    }
                    Err(e) => {
                        eprintln!("JWT refresh failed: {e}");
                        return Err(CliError::AuthExpired);
                    }
                }
            } else if let Some(cookie) = &auth.clerk_client_cookie {
                eprintln!("JWT expired, recovering Clerk session...");
                match auth::clerk_token_exchange(&client, cookie).await {
                    Ok((session_id, jwt)) => {
                        auth.session_id = Some(session_id);
                        auth.jwt = Some(jwt);
                        auth.save()?;
                        eprintln!("JWT refreshed successfully");
                    }
                    Err(e) => {
                        eprintln!("JWT refresh failed: {e}");
                        return Err(CliError::AuthExpired);
                    }
                }
            } else {
                return Err(CliError::AuthExpired);
            }
        }

        Ok(Self {
            client,
            auth: Mutex::new(auth),
        })
    }

    pub(crate) fn get(&self, path: &str) -> reqwest::RequestBuilder {
        self.client
            .get(format!("{BASE_URL}{path}"))
            .headers(self.headers())
    }

    pub(crate) fn post(&self, path: &str) -> reqwest::RequestBuilder {
        self.client
            .post(format!("{BASE_URL}{path}"))
            .headers(self.headers())
    }

    fn headers(&self) -> reqwest::header::HeaderMap {
        let mut headers = reqwest::header::HeaderMap::new();
        // Lock briefly, clone the strings we need, drop the guard before
        // touching the header map. Never hold the lock across an await.
        let (jwt, device) = {
            let auth = self.auth.lock().expect("auth mutex poisoned");
            (
                auth.jwt.clone(),
                auth.device_id
                    .clone()
                    .unwrap_or_else(|| "00000000-0000-0000-0000-000000000000".to_string()),
            )
        };
        if let Some(jwt) = jwt
            && let Ok(val) = format!("Bearer {jwt}").parse()
        {
            headers.insert("authorization", val);
        }
        if let Ok(val) = device.parse() {
            headers.insert("device-id", val);
        }
        if let Ok(val) = auth::browser_token().parse() {
            headers.insert("browser-token", val);
        }
        if let Ok(val) = "https://suno.com".parse() {
            headers.insert("origin", val);
        }
        if let Ok(val) = "https://suno.com/".parse() {
            headers.insert("referer", val);
        }
        headers
    }

    /// Refresh the JWT via the stored Clerk session cookie. Used by the
    /// in-process retry path in `with_auth_retry` when Suno's server-side
    /// staleness check fires mid-request despite a still-valid `exp` claim.
    pub(crate) async fn refresh_jwt(&self) -> Result<(), CliError> {
        // Snapshot the cookie + session_id under the lock, then drop it
        // before the network call.
        let (cookie, session_id) = {
            let auth = self.auth.lock().expect("auth mutex poisoned");
            (
                auth.clerk_client_cookie
                    .clone()
                    .ok_or(CliError::AuthExpired)?,
                auth.session_id.clone(),
            )
        };
        let (session_id, jwt) = if let Some(session_id) = session_id {
            (
                session_id.clone(),
                auth::clerk_refresh_jwt(&self.client, &cookie, &session_id).await?,
            )
        } else {
            auth::clerk_token_exchange(&self.client, &cookie).await?
        };
        // Re-lock briefly to write the new JWT and persist.
        {
            let mut auth = self.auth.lock().expect("auth mutex poisoned");
            auth.session_id = Some(session_id);
            auth.jwt = Some(jwt);
            auth.save()?;
        }
        Ok(())
    }

    /// Run an async API call once. If it fails with `AuthExpired`, refresh
    /// the JWT and try a single retry. Wraps the write/poll paths so
    /// long-running waits (5–30+ minute generation queues) survive Suno's
    /// JWT staleness window.
    pub(crate) async fn with_auth_retry<F, Fut, T>(&self, mut f: F) -> Result<T, CliError>
    where
        F: FnMut() -> Fut,
        Fut: std::future::Future<Output = Result<T, CliError>>,
    {
        match f().await {
            Err(CliError::AuthExpired) => {
                self.refresh_jwt().await?;
                f().await
            }
            other => other,
        }
    }

    pub async fn check_response(
        &self,
        resp: reqwest::Response,
    ) -> Result<reqwest::Response, CliError> {
        let status = resp.status();
        if status == 401 {
            return Err(CliError::AuthExpired);
        }
        if status == 403 {
            let body = resp.text().await.unwrap_or_default();
            if looks_like_auth_expired(&body) {
                return Err(CliError::AuthExpired);
            }
            return Err(CliError::Api {
                code: "forbidden",
                message: format!("HTTP 403 Forbidden: {body}"),
            });
        }
        if status == 429 {
            return Err(CliError::RateLimited);
        }
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            // Map known Suno error patterns to actionable codes so callers
            // get a meaningful suggestion instead of an opaque HTTP dump.
            //
            // `Token validation failed` is what Suno returns when the JWT
            // has crossed their server-side staleness threshold (~30 min)
            // even when the JWT's own `exp` claim is still valid. We treat
            // it as `AuthExpired` so the next CLI invocation will refresh
            // via the Clerk session cookie and pick up a fresh token.
            if looks_like_auth_expired(&body) {
                return Err(CliError::AuthExpired);
            }
            if body.contains("'loc': ['body', 'params'")
                || body.contains("\"loc\": [\"body\", \"params\"")
            {
                return Err(CliError::Api {
                    code: "schema_drift",
                    message: format!(
                        "HTTP {status}: Suno's request schema has changed — the CLI needs an update. Body: {body}"
                    ),
                });
            }
            return Err(CliError::Api {
                code: "api_error",
                message: format!("HTTP {status}: {body}"),
            });
        }
        Ok(resp)
    }
}

fn looks_like_auth_expired(body: &str) -> bool {
    let lower = body.to_ascii_lowercase();
    lower.contains("token validation failed")
        || lower.contains("jwt expired")
        || lower.contains("jwt is expired")
        || lower.contains("invalid jwt")
        || lower.contains("invalid token")
        || lower.contains("not authenticated")
        || lower.contains("unauthenticated")
}
