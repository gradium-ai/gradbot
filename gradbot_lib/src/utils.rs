use anyhow::Result;

#[must_use]
pub struct JoinHandleAbortOnDrop(tokio::task::JoinHandle<()>);

impl Drop for JoinHandleAbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

fn spawn_detach_on_drop<F>(name: &'static str, future: F) -> tokio::task::JoinHandle<()>
where
    F: std::future::Future<Output = Result<()>> + Send + 'static,
{
    tokio::task::spawn(async move {
        match future.await {
            Ok(_) => tracing::info!(?name, "task completed successfully"),
            Err(err) => tracing::error!(?name, ?err, "task failed"),
        }
    })
}

pub fn spawn_abort_on_drop<F>(name: &'static str, future: F) -> JoinHandleAbortOnDrop
where
    F: std::future::Future<Output = Result<()>> + Send + 'static,
{
    let jh = spawn_detach_on_drop(name, future);
    JoinHandleAbortOnDrop(jh)
}
