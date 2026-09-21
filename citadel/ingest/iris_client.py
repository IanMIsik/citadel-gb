"""Elexon IRIS (Insights Real-Time Information Service) consumer.

IRIS is a free push service over Azure Service Bus (AMQP) -- confirmed live
against github.com/elexon-data/iris-clients. Each message's `subject` is
the dataset name (e.g. "BOALF"); a message is removed from the queue once
received, with a 3-day TTL if nothing connects. One connection per queue.

This module only starts if citadel.config.settings.iris_configured is True
(all four of client id/secret/queue name/tenant present) -- the app runs
fine on REST polling alone otherwise (see engine/runner.py), and upgrades
to this the moment credentials are added to .env, no code change needed.

The user must sign up themselves at https://bmrs.elexon.co.uk/iris to get
credentials -- account creation on a third-party site isn't something this
tool does on anyone's behalf.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Awaitable

from azure.identity.aio import ClientSecretCredential
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus.exceptions import ServiceBusError

from ..config import Settings

logger = logging.getLogger("citadel.ingest.iris_client")

# The datasets this project's pricing engine needs -- IRIS delivers many
# more (queue-side filters can narrow this further; see the Insights
# Solution's queue configuration), but only these six matter here.
RELEVANT_DATASETS = {"BOALF", "BOD", "PN", "MELS", "MILS", "DISBSAD"}

MessageHandler = Callable[[str, dict], Awaitable[None]]


async def run_iris_consumer(settings: Settings, on_message: MessageHandler, stop: asyncio.Event) -> None:
    """Runs until `stop` is set. `on_message(dataset, payload)` is awaited
    for every relevant message; the message is only completed (removed
    from the queue) after that handler returns without raising, so a
    processing bug re-delivers the message on reconnect rather than
    silently losing it.
    """
    credential = ClientSecretCredential(settings.iris_tenant_id, settings.iris_client_id, settings.iris_client_secret)
    namespace = f"{settings.iris_namespace}.servicebus.windows.net"

    async with ServiceBusClient(namespace, credential) as client:
        async with client.get_queue_receiver(settings.iris_queue_name) as receiver:
            logger.info("IRIS connected: queue=%s", settings.iris_queue_name)
            while not stop.is_set():
                messages = await receiver.receive_messages(max_message_count=50, max_wait_time=5)
                for msg in messages:
                    dataset = (msg.subject or "unknown").upper()
                    try:
                        if dataset in RELEVANT_DATASETS:
                            body = b"".join(msg.body).decode("utf-8") if hasattr(msg.body, "__iter__") else str(msg.body)
                            payload = json.loads(body)
                            await on_message(dataset, payload)
                        await receiver.complete_message(msg)
                    except Exception:
                        logger.exception("failed to process IRIS message (dataset=%s), abandoning for redelivery", dataset)
                        try:
                            await receiver.abandon_message(msg)
                        except ServiceBusError:
                            logger.exception("failed to abandon IRIS message")
    await credential.close()


async def run_iris_consumer_with_restart(settings: Settings, on_message: MessageHandler, stop: asyncio.Event) -> None:
    """Wraps run_iris_consumer with reconnect-on-failure -- a dropped AMQP
    connection must not permanently end real-time updates for the life of
    the process.
    """
    backoff = 2
    while not stop.is_set():
        try:
            await run_iris_consumer(settings, on_message, stop)
        except Exception:
            logger.exception("IRIS consumer crashed, reconnecting in %ss", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        else:
            backoff = 2
