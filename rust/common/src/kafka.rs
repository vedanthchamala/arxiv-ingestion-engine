use std::time::Duration;

use anyhow::{Context, Result, anyhow};
use rdkafka::config::ClientConfig;
use rdkafka::consumer::StreamConsumer;
use rdkafka::producer::{FutureProducer, FutureRecord};

pub fn producer(brokers: &str) -> Result<FutureProducer> {
    ClientConfig::new()
        .set("bootstrap.servers", brokers)
        .set("acks", "all")
        .set("enable.idempotence", "true")
        .set("compression.type", "gzip")
        .set("message.timeout.ms", "30000")
        .create()
        .context("create kafka producer")
}

/// Consumer tuned for slow, at-least-once processing: manual commits after each message and a
/// poll interval long enough that a 10-minute paper does not trigger a rebalance.
pub fn consumer(brokers: &str, group: &str) -> Result<StreamConsumer> {
    ClientConfig::new()
        .set("bootstrap.servers", brokers)
        .set("group.id", group)
        .set("enable.auto.commit", "false")
        .set("auto.offset.reset", "earliest")
        .set("max.poll.interval.ms", "600000")
        .set("session.timeout.ms", "45000")
        .create()
        .context("create kafka consumer")
}

/// Send pre-serialized bytes; returns (partition, offset).
pub async fn send(producer: &FutureProducer, topic: &str, key: &str, payload: &[u8]) -> Result<(i32, i64)> {
    let record = FutureRecord::to(topic).key(key).payload(payload);
    producer
        .send(record, Duration::from_secs(30))
        .await
        .map(|d| (d.partition, d.offset))
        .map_err(|(e, _)| anyhow!("produce to {topic} key={key}: {e}"))
}
