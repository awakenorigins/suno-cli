use super::SunoClient;
use super::types::Clip;
use crate::errors::CliError;

impl SunoClient {
    /// Extract stems (vocals + instruments) from a clip.
    /// Uses /api/edit/stems/{song_id} per gcui-art/paean-ai evidence.
    pub async fn stems(&self, clip_id: &str) -> Result<Clip, CliError> {
        self.with_auth_retry(|| async {
            let resp = self
                .post(&format!("/api/edit/stems/{clip_id}"))
                .send()
                .await?;
            let resp = self.check_response(resp).await?;
            Ok(resp.json().await?)
        })
        .await
    }
}
