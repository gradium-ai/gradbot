use anyhow::Result;
use clap::Parser;
#[derive(Parser, Debug)]
#[command(name = "Gradbot")]
#[command(about = "A minimalist conversational bot", long_about = None)]
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
    let config = gradbot_bin::Config::load(args.config)?;

    let _guard = tracing_init(
        &config.log_dir,
        &config.instance_name,
        &args.log_level,
        false,
    )?;
    tracing::info!("starting gradbot...");
    match &config.transport {
        gradbot_bin::Transport::WsOpenai => gradbot_bin::openai_server::serve(config).await?,
        gradbot_bin::Transport::Twilio(twilio_config) => {
            gradbot_bin::twilio_server::serve(config.clone(), twilio_config.clone()).await?
        }
    }

    Ok(())
}

fn tracing_init(
    log_dir: &str,
    instance_name: &str,
    log_level: &str,
    silent: bool,
) -> Result<tracing_appender::non_blocking::WorkerGuard> {
    use std::str::FromStr;
    use tracing_subscriber::prelude::*;

    let log_as_json = !matches!(
        std::env::var("LOG_AS_JSON").ok().as_deref(),
        None | Some("0") | Some("false") | Some("")
    );
    let file_appender = tracing_appender::rolling::daily(log_dir, format!("log.{instance_name}"));
    let (non_blocking, guard) = tracing_appender::non_blocking(file_appender);
    let filter = tracing_subscriber::filter::LevelFilter::from_str(log_level)?;
    let fmt = tracing_subscriber::fmt::format()
        .with_file(true)
        .with_line_number(true);
    let log = if log_as_json {
        tracing_subscriber::fmt::layer()
            .event_format(fmt.clone())
            .json()
            .with_writer(non_blocking)
            .with_filter(filter)
            .boxed()
    } else {
        tracing_subscriber::fmt::layer()
            .event_format(fmt.clone())
            .with_writer(non_blocking)
            .with_filter(filter)
            .boxed()
    };
    let mut layers = vec![log];
    if !silent {
        if log_as_json {
            layers.push(Box::new(
                tracing_subscriber::fmt::layer()
                    .event_format(fmt.clone())
                    .json()
                    .with_writer(std::io::stdout)
                    .with_filter(filter),
            ))
        } else {
            layers.push(Box::new(
                tracing_subscriber::fmt::layer()
                    .event_format(fmt.clone())
                    .with_writer(std::io::stdout)
                    .with_filter(filter),
            ))
        }
    };
    tracing_subscriber::registry().with(layers).init();
    Ok(guard)
}
