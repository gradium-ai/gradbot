mod config;
mod protocol;
mod server;

use anyhow::Result;
use clap::Parser;

#[derive(Parser, Debug)]
#[command(name = "gradbot_server")]
#[command(about = "Standalone gradbot WebSocket server")]
struct Args {
    #[clap(short = 'l', long = "log", default_value = "info")]
    log_level: String,

    #[clap(long)]
    config: String,

    #[clap(long)]
    silent: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let config = config::Config::load(&args.config)?;

    let _guard = tracing_init(&config.log_dir, &args.log_level, args.silent)?;
    tracing::info!("starting gradbot_server...");

    server::serve(config).await
}

fn tracing_init(
    log_dir: &str,
    log_level: &str,
    silent: bool,
) -> Result<tracing_appender::non_blocking::WorkerGuard> {
    use std::str::FromStr;
    use tracing_subscriber::prelude::*;

    let file_appender = tracing_appender::rolling::daily(log_dir, "log.gradbot_server");
    let (non_blocking, guard) = tracing_appender::non_blocking(file_appender);
    let filter = tracing_subscriber::filter::LevelFilter::from_str(log_level)?;
    let fmt = tracing_subscriber::fmt::format()
        .with_file(true)
        .with_line_number(true);

    let log = tracing_subscriber::fmt::layer()
        .event_format(fmt.clone())
        .with_writer(non_blocking)
        .with_filter(filter);

    let mut layers: Vec<Box<dyn tracing_subscriber::Layer<_> + Send + Sync>> = vec![Box::new(log)];
    if !silent {
        layers.push(Box::new(
            tracing_subscriber::fmt::layer()
                .event_format(fmt)
                .with_writer(std::io::stdout)
                .with_filter(filter),
        ));
    }
    tracing_subscriber::registry().with(layers).init();
    Ok(guard)
}
