// https://www.twilio.com/docs/voice/media-streams/websocket-messages?utm_source=chatgpt.com#websocket-messages-from-twilio

#[derive(Debug, serde::Serialize, serde::Deserialize, Clone)]
pub struct Start {
    #[serde(rename = "accountSid")]
    pub account_sid: String,
    #[serde(rename = "callSid")]
    pub call_sid: String,
    #[serde(rename = "streamSid")]
    pub stream_sid: String,
    pub tracks: Vec<String>,
    #[serde(rename = "mediaFormat")]
    pub media_format: serde_json::Value,
    #[serde(rename = "customParameters")]
    pub custom_parameters: Option<serde_json::Value>,
}

#[derive(Debug, serde::Serialize, serde::Deserialize, Clone)]
pub struct InboundMedia {
    pub track: String,
    pub chunk: String,
    pub timestamp: String,
    pub payload: String,
}

#[derive(Debug, serde::Serialize, serde::Deserialize, Clone)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum InboundEvent {
    Connected {
        protocol: String,
        version: String,
    },
    Start {
        #[serde(rename = "streamSid")]
        stream_sid: String,
        #[serde(rename = "sequenceNumber")]
        sequence_number: String,
        start: Start,
    },
    Media {
        #[serde(rename = "streamSid")]
        stream_sid: String,
        #[serde(rename = "sequenceNumber")]
        sequence_number: String,
        media: InboundMedia,
    },
    Stop {
        #[serde(rename = "streamSid")]
        stream_sid: String,
        #[serde(rename = "sequenceNumber")]
        sequence_number: String,
    },
    Dtmf {
        #[serde(rename = "streamSid")]
        stream_sid: String,
        #[serde(rename = "sequenceNumber")]
        sequence_number: String,
    },
    Mark {
        #[serde(rename = "streamSid")]
        stream_sid: String,
        #[serde(rename = "sequenceNumber")]
        sequence_number: String,
    },
}

#[derive(Debug, serde::Serialize, serde::Deserialize, Clone)]
pub struct OutboundMedia {
    pub payload: String,
}

#[derive(Debug, serde::Serialize, serde::Deserialize, Clone)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum OutboundEvent {
    Media {
        #[serde(rename = "streamSid")]
        stream_sid: String,
        media: OutboundMedia,
    },
    Clear {
        #[serde(rename = "streamSid")]
        stream_sid: String,
    },
}
