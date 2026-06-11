use asset::Asset;
use async_trait::async_trait;
use std::error::Error;

#[async_trait]
pub trait Source {
    type Config;

    async fn take_snapshot(config: &Self::Config) -> Result<Vec<Asset>, Box<dyn Error>>;
}
