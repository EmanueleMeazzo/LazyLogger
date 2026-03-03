from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .config import Settings

URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)


@dataclass
class LinkExtractionResult:
    url: str
    canonical_url: str
    title: str
    extracted_text: str
    captured_at: str
    domain: str
    note_path: str
    success: bool
    error: str | None = None


class LinkExtractor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def extract_urls(self, text: str) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for match in URL_PATTERN.findall(text):
            url = match.rstrip(".,;:!?)\"")
            if url not in seen:
                seen.add(url)
                urls.append(url)
        return urls

    async def extract(self, url: str) -> LinkExtractionResult:
        parsed = urlparse(url)
        domain = (parsed.hostname or "").lower()
        canonical_url = parsed.geturl()
        captured_at = datetime.now(tz=timezone.utc).isoformat()

        allowed, error = await self._is_allowed_url(canonical_url)
        if not allowed:
            return LinkExtractionResult(
                url=url,
                canonical_url=canonical_url,
                title=domain or "link",
                extracted_text="",
                captured_at=captured_at,
                domain=domain,
                note_path=self._build_note_path(domain, captured_at, canonical_url),
                success=False,
                error=error,
            )

        try:
            title, text = await self._extract_with_crawl4ai(canonical_url)
            note_path = self._build_note_path(domain, captured_at, canonical_url, title)
            return LinkExtractionResult(
                url=url,
                canonical_url=canonical_url,
                title=title,
                extracted_text=text,
                captured_at=captured_at,
                domain=domain,
                note_path=note_path,
                success=True,
            )
        except Exception as exc:
            return LinkExtractionResult(
                url=url,
                canonical_url=canonical_url,
                title=domain or "link",
                extracted_text="",
                captured_at=captured_at,
                domain=domain,
                note_path=self._build_note_path(domain, captured_at, canonical_url),
                success=False,
                error=f"Extraction failed: {exc}",
            )

    async def _is_allowed_url(self, url: str) -> tuple[bool, str | None]:
        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"}:
            return False, "Only http/https links are supported"

        if not parsed.hostname:
            return False, "Invalid URL hostname"

        hostname = parsed.hostname.lower()
        blocked = self.settings.get_blocked_domains()
        allowed = self.settings.get_allowed_domains()

        if self._matches_domain_list(hostname, blocked):
            return False, "Domain is blocked"

        if allowed and not self._matches_domain_list(hostname, allowed):
            return False, "Domain is not in allowed list"

        if self.settings.url_allow_private_nets:
            return True, None

        resolved_ips = await asyncio.to_thread(self._resolve_ips, hostname)
        if not resolved_ips:
            return False, "Could not resolve host"

        for ip in resolved_ips:
            if self._is_private_or_local_ip(ip):
                return False, "URL points to a private or local network"

        return True, None

    def _resolve_ips(self, hostname: str) -> set[str]:
        addresses: set[str] = set()
        for entry in socket.getaddrinfo(hostname, None):
            sockaddr = entry[4]
            if sockaddr:
                addresses.add(sockaddr[0])
        return addresses

    def _matches_domain_list(self, hostname: str, domains: set[str]) -> bool:
        for domain in domains:
            if hostname == domain or hostname.endswith(f".{domain}"):
                return True
        return False

    def _is_private_or_local_ip(self, raw_ip: str) -> bool:
        ip = ipaddress.ip_address(raw_ip)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        )

    async def _extract_with_crawl4ai(self, url: str) -> tuple[str, str]:
        try:
            from crawl4ai import AsyncWebCrawler
        except ImportError as exc:
            raise RuntimeError(
                "Crawl4AI is not installed in this environment"
            ) from exc

        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(
                url=url,
                page_timeout=self.settings.url_fetch_timeout_seconds * 1000,
            )

        success = bool(getattr(result, "success", False))
        if not success:
            error_message = getattr(result, "error_message", "unknown error")
            raise RuntimeError(str(error_message))

        title = self._pick_first_text(
            self._extract_nested(result, "metadata", "title"),
            getattr(result, "title", None),
            urlparse(url).hostname,
            "untitled",
        )
        extracted_text = self._pick_first_text(
            getattr(result, "markdown", None),
            getattr(result, "cleaned_html", None),
            getattr(result, "html", None),
            getattr(result, "extracted_content", None),
            "",
        )

        clipped_text = extracted_text[: self.settings.url_fetch_max_chars]
        if not clipped_text.strip():
            raise RuntimeError("No extractable text content")

        return title, clipped_text

    def _extract_nested(self, obj: object, attr: str, key: str) -> str | None:
        nested = getattr(obj, attr, None)
        if isinstance(nested, dict):
            value = nested.get(key)
            return str(value) if value else None
        return None

    def _pick_first_text(self, *values: object) -> str:
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    def _build_note_path(
        self,
        domain: str,
        captured_at: str,
        canonical_url: str,
        title: str | None = None,
    ) -> str:
        now = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        slug_seed = title or domain or canonical_url
        slug = self._slugify(slug_seed)
        digest = hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()[:8]
        file_name = f"{now.year}{now.month:02d}{now.day:02d}-{slug}-{digest}.md"
        return f"{self.settings.link_notes_folder}/{now.year}/{now.month:02d}/{file_name}"

    def _slugify(self, text: str) -> str:
        lowered = text.lower()
        normalized = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
        if not normalized:
            return "link"
        return normalized[:60]
