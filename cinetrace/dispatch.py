"""Optional Kafka notifications. SQLite owns the durable jobs and retry state."""

import logging
import os
import threading
from contextlib import contextmanager


log = logging.getLogger(__name__)
TOPIC = "cinetrace.context"


@contextmanager
def kafka_notifications(store, wake):
    brokers = os.getenv("CINETRACE_KAFKA")
    if not brokers:
        yield
        return

    stop = threading.Event()
    thread = threading.Thread(target=_relay, args=(brokers, store, wake, stop), daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


def _relay(brokers, store, wake, stop):
    try:
        from confluent_kafka import Consumer, Producer
    except ImportError:
        log.warning("Kafka 已配置但未安装 confluent-kafka，任务保留在本地队列。")
        return

    consumer = Consumer({"bootstrap.servers": brokers, "group.id": "cinetrace-local",
                         "auto.offset.reset": "earliest"})
    producer = Producer({"bootstrap.servers": brokers, "message.timeout.ms": 3000})
    consumer.subscribe([TOPIC])
    in_flight = set()

    def delivered(error, message):
        video_id = message.key().decode()
        in_flight.discard(video_id)
        if error is None:
            store.mark_notified(video_id)

    try:
        while not stop.is_set():
            for video_id in store.pending_notifications():
                if video_id not in in_flight:
                    producer.produce(TOPIC, key=video_id, value=video_id, on_delivery=delivered)
                    in_flight.add(video_id)
            producer.poll(0)
            event = consumer.poll(0.4)
            if event is not None and not event.error():
                wake.set()
    finally:
        producer.flush(1)
        consumer.close()
