#!/usr/bin/env python3
"""Build clean data.json from upstream RSS feed (no upstream branding)."""
import html
import json
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

BRAND = "Gz"
STATUS_FA = {
    "investigating": "در حال بررسی",
    "identified": "مشکل شناسایی شد",
    "monitoring": "پایش وضعیت",
    "resolved": "حل شد",
}
COMP_FA = {
    "operational": "فعال",
    "degraded performance": "کاهش عملکرد",
    "partial outage": "قطعی نسبی",
    "full outage": "قطعی کامل",
    "maintenance": "تعمیرات",
}

feed_file = Path("feed.rss")
if not feed_file.exists() or feed_file.stat().st_size < 100:
    print("feed missing or too small; keeping previous data.json")
    sys.exit(0)

raw = feed_file.read_text(encoding="utf-8", errors="replace")
if "gozargah" in raw.lower():
    # content came back but upstream branding present; we only strip names anyway
    pass

try:
    root = ElementTree.fromstring(raw)
except ElementTree.ParseError:
    print("ERROR: feed is not valid XML; keeping previous data.json")
    sys.exit(0)

channel = root.find("channel")
if channel is None:
    print("ERROR: no channel in feed")
    sys.exit(0)


def parse_dt(s):
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except Exception:
        return None


def strip_tags(fragment):
    text = re.sub(r"<br\s*/?>", "\n", fragment or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return text.strip()


def parse_components(desc_html):
    comps = []
    block = re.search(r"<b>Affected components</b>\s*<ul>(.*?)</ul>", desc_html or "", re.S)
    if block:
        for m in re.finditer(r"<li>(.*?)</li>", block.group(1), re.S):
            item = strip_tags(m.group(1))
            mm = re.match(r"(.+?)\s*\((.+)\)\s*$", item)
            if mm:
                name = mm.group(1).strip()
                st = mm.group(2).strip().lower()
                comps.append({
                    "name": name,
                    "name_fa": {"Germany": "آلمان", "US": "آمریکا", "Finland": "فنلاند"}.get(name, name),
                    "status": st,
                    "status_fa": COMP_FA.get(st, st),
                })
    return comps


incidents = []
for item in channel.findall("item"):
    title = strip_tags(item.findtext("title") or "")
    link = (item.findtext("link") or "").strip()
    pub = item.findtext("pubDate")
    dt = parse_dt(pub) if pub else None
    desc_html = item.findtext("description") or ""
    content_html = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""
    body_html = content_html or desc_html

    status_en = ""
    m = re.search(r"<b>Status:\s*([^<]+)</b>", body_html)
    if m:
        status_en = m.group(1).strip()

    message = body_html
    message = re.sub(r"<b>Status:[^<]*</b>", "", message)
    message = re.sub(r"<b>Affected components</b>.*?</ul>", "", message, flags=re.S)
    message = strip_tags(message)

    comps = parse_components(body_html)
    incidents.append({
        "id": re.sub(r"[^A-Za-z0-9]", "", link[-26:]) or f"inc-{len(incidents)}",
        "title": title,
        "status": status_en.lower() if status_en else STATUS_FA.get(status_en.lower(), "—"),
        "status_fa": STATUS_FA.get(status_en.lower(), status_en or "—"),
        "message": message,
        "components": comps,
        "published_at": dt.isoformat() if dt else (pub or ""),
        "published_fa": dt.strftime("%Y-%m-%d %H:%M") + " UTC" if dt else "",
    })

data = {
    "brand": BRAND,
    "updated_at": datetime.now(timezone.utc).isoformat(),
    "overall": "operational" if all(i["status"] == "resolved" for i in incidents[:1]) and (not incidents or incidents[0]["status"] == "resolved") else "active",
    "incidents": incidents[:20],
}

Path("data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"OK: {len(incidents)} incidents -> data.json")
