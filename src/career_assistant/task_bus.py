"""Optional Kafka transport for the existing SQLite task state machine.

SQLite remains the source of truth. A transactional outbox prevents a task row
from being committed without its Kafka event. Consumers acknowledge only after
the task reaches a terminal state; duplicate delivery is absorbed by task status
and idempotency.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
from typing import Callable

from career_assistant.models import now_hk, parse_iso
from .store import Store, encode
from .worker import Worker, safe_error


LOG = logging.getLogger(__name__)
TERMINAL_TASK_STATUSES = {"succeeded", "failed", "uncertain"}


def configured_task_backend() -> str:
    backend = os.environ.get("TASK_BACKEND", "sqlite").strip().lower()
    if backend not in {"sqlite", "kafka"}:
        raise ValueError("TASK_BACKEND 只支持 sqlite 或 kafka")
    return backend


@dataclass(frozen=True)
class KafkaSettings:
    bootstrap_servers: str = "127.0.0.1:9092"
    task_topic: str = "career-assistant.tasks"
    dlq_topic: str = "career-assistant.tasks.dlq"
    consumer_group: str = "career-assistant-workers"
    client_id: str = "career-assistant"
    poll_seconds: float = 1.0
    outbox_poll_seconds: float = 1.0
    publish_timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> "KafkaSettings":
        settings = cls(
            bootstrap_servers=os.environ.get(
                "KAFKA_BOOTSTRAP_SERVERS", cls.bootstrap_servers
            ).strip(),
            task_topic=os.environ.get("KAFKA_TASK_TOPIC", cls.task_topic).strip(),
            dlq_topic=os.environ.get("KAFKA_DLQ_TOPIC", cls.dlq_topic).strip(),
            consumer_group=os.environ.get(
                "KAFKA_CONSUMER_GROUP", cls.consumer_group
            ).strip(),
            client_id=os.environ.get("KAFKA_CLIENT_ID", cls.client_id).strip(),
            poll_seconds=max(0.1, float(os.environ.get("KAFKA_POLL_SECONDS", cls.poll_seconds))),
            outbox_poll_seconds=max(
                0.2,
                float(os.environ.get("OUTBOX_POLL_SECONDS", cls.outbox_poll_seconds)),
            ),
            publish_timeout_seconds=max(
                1.0,
                float(os.environ.get(
                    "KAFKA_PUBLISH_TIMEOUT_SECONDS", cls.publish_timeout_seconds
                )),
            ),
        )
        for name in ("bootstrap_servers", "task_topic", "dlq_topic", "consumer_group", "client_id"):
            if not getattr(settings, name):
                raise ValueError(f"Kafka 配置 {name} 不能为空")
        return settings


def _confluent():
    try:
        import confluent_kafka
        from confluent_kafka.admin import AdminClient, NewTopic
    except ImportError as exc:
        raise RuntimeError(
            "Kafka 模式需要安装 confluent-kafka；请运行 pip install -r requirements.txt"
        ) from exc
    return confluent_kafka, AdminClient, NewTopic


class KafkaTaskBus:
    def __init__(self, settings: KafkaSettings | None = None):
        self.settings = settings or KafkaSettings.from_env()
        self._producer = None

    def ensure_topics(self) -> None:
        _, AdminClient, NewTopic = _confluent()
        admin = AdminClient({"bootstrap.servers": self.settings.bootstrap_servers})
        existing = set(admin.list_topics(timeout=15).topics)
        wanted = [
            topic for topic in (self.settings.task_topic, self.settings.dlq_topic)
            if topic not in existing
        ]
        if not wanted:
            return
        futures = admin.create_topics(
            [NewTopic(topic, num_partitions=1, replication_factor=1) for topic in wanted],
            operation_timeout=15,
        )
        for topic, future in futures.items():
            try:
                future.result(timeout=15)
            except Exception as exc:
                # Another process may have created it between list and create.
                if "TOPIC_ALREADY_EXISTS" not in str(exc):
                    raise RuntimeError(f"创建 Kafka topic {topic} 失败：{exc}") from exc

    def _get_producer(self):
        if self._producer is not None:
            return self._producer
        confluent_kafka, _, _ = _confluent()
        self._producer = confluent_kafka.Producer({
            "bootstrap.servers": self.settings.bootstrap_servers,
            "client.id": self.settings.client_id + "-outbox",
            "acks": "all",
            "enable.idempotence": True,
            "retries": 5,
            "socket.timeout.ms": 10000,
            "message.timeout.ms": int(self.settings.publish_timeout_seconds * 1000),
        })
        return self._producer

    def _publish(self, topic: str, key: str, value: dict) -> None:
        producer = self._get_producer()
        delivered = threading.Event()
        failure: list[str] = []

        def on_delivery(error, _message):
            if error is not None:
                failure.append(str(error))
            delivered.set()

        producer.produce(
            topic,
            key=key.encode("utf-8"),
            value=encode(value).encode("utf-8"),
            on_delivery=on_delivery,
        )
        deadline = time.monotonic() + self.settings.publish_timeout_seconds
        while not delivered.is_set() and time.monotonic() < deadline:
            producer.poll(0.1)
        if not delivered.is_set():
            raise TimeoutError(f"Kafka 消息在 {self.settings.publish_timeout_seconds:g}s 内未确认")
        if failure:
            raise RuntimeError(f"Kafka 发送失败：{failure[0]}")

    def publish_task_event(self, event: dict) -> None:
        self._publish(
            self.settings.task_topic,
            str(event["task_id"]),
            event,
        )

    def publish_dlq(self, *, message: dict, error: str) -> None:
        self._publish(
            self.settings.dlq_topic,
            str(message.get("task_id", "unknown")),
            {
                "schema_version": 1,
                "source_topic": self.settings.task_topic,
                "error": str(error)[:2000],
                "message": message,
            },
        )

    def close(self) -> None:
        if self._producer is not None:
            self._producer.flush(5)
            self._producer = None


class OutboxRelay:
    """Move committed SQLite outbox rows to Kafka with at-least-once delivery."""

    def __init__(
        self,
        store: Store,
        bus: KafkaTaskBus,
        *,
        poll_seconds: float = 1.0,
        batch_size: int = 20,
    ):
        self.store = store
        self.bus = bus
        self.poll_seconds = max(0.2, poll_seconds)
        self.batch_size = batch_size
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="career-outbox-relay",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def run_once(self) -> int:
        self.store.ensure_queued_outbox()
        published = 0
        for row in self.store.claim_outbox_batch(self.batch_size):
            task = self.store.get("tasks", row["task_id"])
            if task is None:
                self.store.mark_outbox_failed(row["id"], "任务记录不存在", 30)
                continue
            event = self.store.task_event(task)
            try:
                self.bus.publish_task_event(event)
            except Exception as exc:
                backoff = min(30, max(2, 2 ** min(int(row["attempts"]), 4)))
                self.store.mark_outbox_failed(row["id"], safe_error(exc), backoff)
                LOG.warning("outbox %s 发送失败：%s", row["id"], safe_error(exc))
                continue
            self.store.mark_outbox_published(row["id"])
            published += 1
        return published

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sent = self.run_once()
            except Exception:
                LOG.exception("outbox relay 循环失败")
                sent = 0
            if not sent:
                self._stop.wait(self.poll_seconds)


def _validate_task_event(event: object, task: dict) -> dict:
    if not isinstance(event, dict):
        raise ValueError("消息体必须是 JSON 对象")
    required = {"schema_version", "task_id", "kind", "idempotency_key"}
    if set(event) != required:
        raise ValueError("消息字段不符合任务事件结构")
    if event["schema_version"] != 1:
        raise ValueError("不支持的任务事件 schema_version")
    try:
        task_id = int(event["task_id"])
    except (TypeError, ValueError) as exc:
        raise ValueError("task_id 必须是整数") from exc
    if task_id != task["id"]:
        raise ValueError("task_id 与数据库记录不一致")
    if event["kind"] != task["kind"]:
        raise ValueError("kind 与数据库记录不一致")
    if event["idempotency_key"] != task["idempotency_key"]:
        raise ValueError("idempotency_key 与数据库记录不一致")
    return {**event, "task_id": task_id}


class KafkaTaskConsumer:
    def __init__(
        self,
        store: Store,
        worker: Worker,
        bus: KafkaTaskBus,
        *,
        stop_event: threading.Event | None = None,
    ):
        self.store = store
        self.worker = worker
        self.bus = bus
        self.settings = bus.settings
        self.stop_event = stop_event or threading.Event()
        self._consumer = None

    def _get_consumer(self):
        if self._consumer is not None:
            return self._consumer
        confluent_kafka, _, _ = _confluent()
        self._consumer = confluent_kafka.Consumer({
            "bootstrap.servers": self.settings.bootstrap_servers,
            "group.id": self.settings.consumer_group,
            "client.id": self.settings.client_id + "-consumer",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "max.poll.interval.ms": 1800000,
            "session.timeout.ms": 45000,
        })
        self._consumer.subscribe([self.settings.task_topic])
        return self._consumer

    def run_forever(self) -> None:
        consumer = self._get_consumer()
        try:
            while not self.stop_event.is_set():
                message = consumer.poll(self.settings.poll_seconds)
                if message is None:
                    continue
                if message.error():
                    LOG.error("Kafka consumer 错误：%s", message.error())
                    continue
                try:
                    self._handle_message(message)
                except Exception as exc:
                    LOG.exception("Kafka 消息处理失败，未提交 offset：%s", safe_error(exc))
                    self._rewind(consumer, message)
                    self.stop_event.wait(2)
        finally:
            consumer.close()
            self._consumer = None

    def _decode(self, message) -> tuple[dict | None, str]:
        raw = message.value()
        if raw is None:
            return None, "消息体为空"
        try:
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"消息不是有效 UTF-8 JSON：{exc}"
        if not isinstance(event, dict):
            return None, "消息体必须是 JSON 对象"
        return event, ""

    def _handle_message(self, message) -> None:
        event, decode_error = self._decode(message)
        if decode_error:
            self.bus.publish_dlq(message={"raw": "<invalid-json>"}, error=decode_error)
            self._commit(message)
            return
        try:
            task_id = int(event.get("task_id"))
        except (TypeError, ValueError):
            self.bus.publish_dlq(message=event, error="task_id 缺失或不是整数")
            self._commit(message)
            return

        while not self.stop_event.is_set():
            task = self.store.get("tasks", task_id)
            if task is None:
                self.bus.publish_dlq(message=event, error="任务记录不存在")
                self._commit(message)
                return
            try:
                event = _validate_task_event(event, task)
            except ValueError as exc:
                self.bus.publish_dlq(message=event, error=str(exc))
                self._commit(message)
                return
            if task["status"] in TERMINAL_TASK_STATUSES:
                self._commit(message)
                return
            if task["status"] == "queued":
                available_at = parse_iso(task["available_at"])
                if available_at and available_at > now_hk():
                    self.stop_event.wait(min(2.0, max(
                        0.1,
                        (available_at - now_hk()).total_seconds(),
                    )))
                    continue
                self.worker.process_task_by_id(task_id)
                continue
            # A running row can only be left by a crashed owner. Worker.open()
            # recovers it before consumption starts; otherwise wait for it.
            self.stop_event.wait(0.5)

    def _commit(self, message) -> None:
        self._get_consumer().commit(message=message, asynchronous=False)

    @staticmethod
    def _rewind(consumer, message) -> None:
        _, _, _ = _confluent()
        TopicPartition = __import__(
            "confluent_kafka", fromlist=["TopicPartition"]
        ).TopicPartition
        consumer.seek(TopicPartition(
            message.topic(),
            message.partition(),
            message.offset(),
        ))


class KafkaTaskWorker:
    """Long-running Kafka worker: one consumer and one outbox relay."""

    def __init__(
        self,
        store: Store,
        handlers: dict[str, Callable],
        settings: KafkaSettings | None = None,
    ):
        self.settings = settings or KafkaSettings.from_env()
        self.worker = Worker(store, handlers)
        self.bus = KafkaTaskBus(self.settings)
        self.relay = OutboxRelay(
            store,
            self.bus,
            poll_seconds=self.settings.outbox_poll_seconds,
        )
        self.stop_event = threading.Event()
        self.consumer = KafkaTaskConsumer(
            store,
            self.worker,
            self.bus,
            stop_event=self.stop_event,
        )

    def serve(self) -> None:
        previous_handlers = {}

        def stop_handler(_signum, _frame):
            self.stop_event.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[signum] = signal.signal(signum, stop_handler)
            except (ValueError, OSError):
                pass
        try:
            self.worker.open()
            self.bus.ensure_topics()
            self.relay.start()
            LOG.info(
                "Kafka worker 已启动：%s / %s",
                self.settings.bootstrap_servers,
                self.settings.task_topic,
            )
            self.consumer.run_forever()
        finally:
            self.stop_event.set()
            self.relay.stop()
            self.worker.close()
            self.bus.close()
            for signum, handler in previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except (ValueError, OSError):
                    pass
