use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use anyhow::Result;
use log::{info, warn};

use crate::service::ArchiveService;
use crate::store::ArchiveStore;
use crate::types::FillAttemptResult;

const CLAIM_TTL: Duration = Duration::from_secs(300);
const IDLE_WAIT: Duration = Duration::from_secs(30);

pub struct ArchiveFiller {
    store: Arc<dyn ArchiveStore>,
    service: Arc<ArchiveService>,
    worker_id: String,
    lease_counter: AtomicU64,
}

impl ArchiveFiller {
    pub fn new(store: Arc<dyn ArchiveStore>, service: Arc<ArchiveService>) -> Self {
        let hostname = std::env::var("HOSTNAME").unwrap_or_else(|_| "local".to_string());
        Self {
            store,
            service,
            worker_id: format!("{hostname}:{}", std::process::id()),
            lease_counter: AtomicU64::new(0),
        }
    }

    pub async fn run_forever(&self) -> Result<()> {
        info!("filler worker started id={}", self.worker_id);
        loop {
            match self.run_once().await {
                Ok(true) => {}
                Ok(false) => {
                    let _ = self.store.wait_for_fill_queue_change(IDLE_WAIT).await;
                }
                Err(error) => {
                    warn!("filler loop failed error={error:#}");
                    tokio::time::sleep(Duration::from_secs(1)).await;
                }
            }
        }
    }

    pub async fn run_once(&self) -> Result<bool> {
        let owner = self.next_owner();
        let Some(job) = self.store.claim_next_fill(&owner, CLAIM_TTL).await? else {
            return Ok(false);
        };
        let key = job.request.lease_key();
        let endpoint = job.request.endpoint();
        info!("claimed fill endpoint={} key={}", endpoint.as_str(), key);
        match self.service.process_fill_request(job.request.clone()).await {
            FillAttemptResult::Completed => {
                self.store.complete_fill(&job).await?;
                info!("completed fill endpoint={} key={}", endpoint.as_str(), key);
            }
            FillAttemptResult::RetryAfter {
                retry_after,
                status,
            } => {
                self.store
                    .retry_fill(&job, retry_after, status, Some("retryable fill result"))
                    .await?;
                warn!(
                    "retryable fill endpoint={} key={} status={} retry_after={:?}",
                    endpoint.as_str(),
                    key,
                    status
                        .map(|status| status.to_string())
                        .unwrap_or_else(|| "none".to_string()),
                    retry_after
                );
            }
        }
        Ok(true)
    }

    fn next_owner(&self) -> String {
        let sequence = self.lease_counter.fetch_add(1, Ordering::Relaxed);
        format!("{}:{sequence}", self.worker_id)
    }
}
