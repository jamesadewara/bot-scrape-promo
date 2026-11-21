"""Sitemap reading (C7, F1). xml.etree only; entries carry loc and optional lastmod."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class SitemapEntry:
    loc: str
    lastmod: str | None


@dataclass
class Sitemap:
    is_index: bool
    entries: list[SitemapEntry]


def parse_sitemap(text: str) -> Sitemap:
    # strip namespaces so tags read as <url>, <loc>, <sitemap>
    text = re.sub(r'\sxmlns(:\w+)?="[^"]+"', "", text, count=5)
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return Sitemap(False, [])
    tag = root.tag.split("}")[-1].lower()
    is_index = tag == "sitemapindex"
    child = "sitemap" if is_index else "url"
    out: list[SitemapEntry] = []
    for el in root.iter():
        if el.tag.split("}")[-1].lower() != child:
            continue
        loc = el.findtext("loc")
        if not loc:
            continue
        out.append(SitemapEntry(loc.strip(), (el.findtext("lastmod") or "").strip() or None))
    return Sitemap(is_index, out)


# Child sitemaps most likely to list category / campaign pages come first.
# Measured on the first live run (2026-11-21): the static index lists categories-sitemap.xml.gz and
# mlps-sitemap*.xml.gz; the /slp/ index lists ~6.6 MB files; the blog index lists blog posts only.
_PREFER = ("categor", "mlp", "landing", "campaign", "catalog", "brand")


def child_sitemap_rank(url: str) -> int:
    low = url.lower()
    if "/blog/" in low:
        return 98  # blog posts are not listing or product pages
    if "product" in low:
        return 97  # product pages are fetched only when needed (6.3)
    if "/slp/" in low:
        return 90  # very large files, few allowed listing URLs
    for i, word in enumerate(_PREFER):
        if word in low:
            return i
    return 50
