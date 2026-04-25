"""Notification business logic."""

import logging

logger = logging.getLogger("flowsync.services.notification")


async def send_notification(
    depot_id: str,
    channel: str,
    recipient: str,
    message: str,
) -> dict[str, str]:
    """Send a notification through a selected channel (placeholder implementation)."""
    logger.info(f"[{depot_id}] notify: channel={channel}, recipient={recipient}")
    return {"status": "queued", "channel": channel, "recipient": recipient, "message": message}
