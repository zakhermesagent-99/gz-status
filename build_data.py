#!/usr/bin/env python3
"""Build clean data.json from upstream feeds (no upstream branding).
v2: per-incident update timelines + component status segments for bar chart."""
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen
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
LOC_FA = {"Germany": "آلمان", "US": "آمریکا", "Finland": "فنلاند"}

UA = {"User-Agent": "gz-status/2.0"}


def fetch(url, timeout=30):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_dt(s):
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except Exception:
        return None


def strip_tags(fragment):
    text = re.sub(r"<br\s*/?>", "\n", fragment or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


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
                    "name_fa": LOC_FA.get(name, name),
                    "status": st,
                    "status_fa": COMP_FA.get(st, st),
                })
    return comps


def feed_items(xml_text):
    """Return list of (status_en, message, dt, comps) from an RSS feed, ascending by time."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    channel = root.find("channel")
    if channel is None:
        return []
    out = []
    for item in channel.findall("item"):
        pub = item.findtext("pubDate")
        dt = parse_dt(pub) if pub else None
        body = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or item.findtext("description") or ""
        m = re.search(r"<b>Status:\s*([^<]+)</b>", body)
        status_en = m.group(1).strip().lower() if m else ""
        msg = body
        msg = re.sub(r"<b>Status:[^<]*</b>", "", msg)
        msg = re.sub(r"<b>Affected components</b>.*?</ul>", "", msg, flags=re.S)
        msg = strip_tags(msg)
        comps = parse_components(body)
        out.append({"status": status_en, "message": msg, "dt": dt, "comps": comps})
    out.sort(key=lambda u: u["dt"] or datetime.min.replace(tzinfo=timezone.utc))
    return out


def build_incident_detail(incident_id):
    """Fetch incident-specific feed; return (updates, chart_segments) or (None, None)."""
    url = os.environ.get("UPSTREAM_BASE", "").rstrip("/") + f"/incidents/{incident_id}/feed.rss"
    if "raw.invalid" in url:
        return None, None
    try:
        xml_text = fetch(url)
    except Exception as e:
        print(f"  warn: incident feed fetch failed: {e}")
        return None, None
    updates_raw = feed_items(xml_text)
    if not updates_raw:
        return None, None

    updates = []
    for u in updates_raw:
        dt = u["dt"]
        updates.append({
            "status": u["status"],
            "status_fa": STATUS_FA.get(u["status"], u["status"]),
            "message": u["message"],
            "components": u["comps"],
            "published_at": dt.isoformat() if dt else "",
            "published_fa": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
        })

    # Build per-component segments for the bar chart
    # For each component: walk updates ascending; status valid until next update that mentions it;
    # chart window = first update time .. final update time (incident end).
    window_start = updates[0]["published_at"]
    window_end = updates[-1]["published_at"]
    comp_names = []
    for u in updates:
        for c in u["components"]:
            if c["name"] not in comp_names:
                comp_names.append(c["name"])

    charts = []
    for name in comp_names:
        segs = []
        cur = None
        seg_start = None
        for u in updates:
            st = None
            for c in u["components"]:
                if c["name"] == name:
                    st = c["status"]
                    break
            if st is None:
                continue
            t = u["published_at"]
            if cur is None:
                cur, seg_start = st, t
            elif st != cur:
                segs.append({"start": seg_start, "end": t, "status": cur, "status_fa": COMP_FA.get(cur, cur)})
                cur, seg_start = st, t
        if cur is not None:
            segs.append({"start": seg_start, "end": window_end, "status": cur, "status_fa": COMP_FA.get(cur, cur)})
        if segs:
            display_name = LOC_FA.get(name, name)
            charts.append({"name": name, "name_fa": display_name,
                           "segments": segs, "start": window_start, "end": window_end})
    return updates, charts


def main():
    src = os.environ.get("UPSTREAM_RSS", "")
    if not src or "raw.invalid" in src:
        # local test mode
        src = sys.argv[1] if len(sys.argv) > 1 else "feed.rss"
        raw = Path(src).read_text(encoding="utf-8", errors="replace") if Path(src).exists() else ""
    else:
        raw = fetch(src)

    if len(raw) < 100:
        print("feed missing/too small; keeping previous data.json")
        sys.exit(0)
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        print("ERROR: feed not valid XML; keeping previous data.json")
        sys.exit(0)
    channel = root.find("channel")
    if channel is None:
        print("ERROR: no channel")
        sys.exit(0)

    incidents = []
    for item in channel.findall("item"):
        title = strip_tags(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate")
        dt = parse_dt(pub) if pub else None
        body = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or item.findtext("description") or ""
        m = re.search(r"<b>Status:\s*([^<]+)</b>", body)
        status_en = m.group(1).strip().lower() if m else ""
        msg = body
        msg = re.sub(r"<b>Status:[^<]*</b>", "", msg)
        msg = re.sub(r"<b>Affected components</b>.*?</ul>", "", msg, flags=re.S)
        msg = strip_tags(msg)
        inc_id = re.sub(r"[^A-Za-z0-9]", "", link[-26:]) or f"inc-{len(incidents)}"

        updates, charts = build_incident_detail(inc_id)

        incidents.append({
            "id": inc_id,
            "title": title,
            "status": status_en,
            "status_fa": STATUS_FA.get(status_en, status_en or "—"),
            "message": msg,
            "components": parse_components(body),
            "published_at": dt.isoformat() if dt else (pub or ""),
            "published_fa": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
            "updates": updates or [],
            "charts": charts or [],
            "ongoing": status_en not in ("resolved",),
        })

    active = any(i["ongoing"] for i in incidents)
    data = {
        "brand": BRAND,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "overall": "active" if active else "operational",
        "incidents": incidents[:20],
    }
    Path("data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {len(incidents)} incidents, ongoing={active}")


if __name__ == "__main__":
    main()
