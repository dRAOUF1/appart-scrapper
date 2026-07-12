"""ntfy.sh notification sender for new listing alerts."""

from __future__ import annotations

import requests
from loguru import logger


class Notifier:
    """Send push notifications via ntfy.sh."""

    def __init__(self, server: str = "https://ntfy.sh", priority: str = "default"):
        self.server = server.rstrip("/")
        self.priority = priority

    def _endpoint(self, topic: str) -> str:
        return f"{self.server}/{topic}"

    def _sanitize_header(self, value: str) -> str:
        """Remove non-ASCII characters from header values (HTTP headers are latin-1)."""
        return value.encode("ascii", errors="replace").decode("ascii")

    def send(self, topic: str, title: str, message: str, url: str = "", priority: str = "", tags: str = "house") -> bool:
        """
        Send a notification via ntfy to a specific topic.

        Args:
            topic: ntfy topic to send to
            title: Notification title
            message: Notification body
            url: Optional click URL
            priority: Override default priority
            tags: Comma-separated emoji tags

        Returns:
            True if sent successfully
        """
        headers = {
            "Title": self._sanitize_header(title),
            "Priority": priority or self.priority,
            "Tags": tags,
        }

        if url:
            headers["Click"] = url
            headers["Actions"] = f"view, Voir l'annonce, {url}"

        try:
            resp = requests.post(
                self._endpoint(topic),
                data=message.encode("utf-8"),
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                logger.debug(f"Notification envoyee [{topic}]: {title}")
                return True
            else:
                logger.warning(f"Erreur ntfy ({resp.status_code}): {resp.text}")
                return False
        except requests.RequestException as e:
            logger.error(f"Erreur reseau ntfy : {e}")
            return False

    def notify_new_listing(self, topic: str, listing) -> bool:
        """Send a formatted notification for a new listing."""
        # Build a clean message with price, location, agency
        parts = []
        if listing.agency:
            parts.append(f"Agence: {listing.agency}")
        if listing.price:
            parts.append(f"Prix: {listing.price}")
        if listing.surface:
            parts.append(f"Surface: {listing.surface}")
        if listing.rooms:
            parts.append(f"Pieces: {listing.rooms}")
        if listing.location:
            parts.append(f"Lieu: {listing.location}")

        message = "\n".join(parts) if parts else "Nouvelle annonce disponible"

        title = "Nouvelle annonce SeLoger"

        return self.send(
            topic=topic,
            title=title,
            message=message,
            url=listing.url,
            priority="high",
            tags="house,new",
        )

    def notify_summary(self, topic: str, new_count: int, total_scanned: int, search_url: str = "") -> bool:
        """Send a summary notification after a scan cycle."""
        if new_count == 0:
            return True  # Don't spam when nothing is new

        title = f"{new_count} nouvelle{'s' if new_count > 1 else ''} annonce{'s' if new_count > 1 else ''}"
        message = f"{total_scanned} annonces scannees, {new_count} nouvelle{'s' if new_count > 1 else ''}"

        return self.send(
            topic=topic,
            title=title,
            message=message,
            url=search_url,
            tags="bell",
        )

    def send_test(self, topic: str) -> bool:
        """Send a test notification to verify configuration."""
        return self.send(
            topic=topic,
            title="SeLoger Scraper - Test",
            message="Les notifications fonctionnent ! Le scraper est pret.",
            tags="white_check_mark",
            priority="low",
        )
