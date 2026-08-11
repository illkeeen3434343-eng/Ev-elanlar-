#!/usr/bin/env python3
"""
Bina.az -> Telegram apartment monitor (single-run edition for GitHub Actions).

This script runs ONCE per invocation: it fetches the newest bina.az listings via
their public GraphQL API, keeps only the ones matching your saved search, and
sends a Telegram message for any it has never seen before. "Seen" ids are stored
in seen.json, which the GitHub Actions workflow commits back to the repo so the
memory survives between runs.

You normally only edit the two lines marked  # <-- EDIT  below.
Secrets (bot token, chat id) are NOT in this file; they come from environment
variables set as GitHub Actions Secrets.
"""
import datetime as dt
import html
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

import requests

# ---------------------------------------------------------------------------
# SETTINGS you may edit
# ---------------------------------------------------------------------------
# Your bina.az search URL (copy it from your browser's address bar):
BINA_SEARCH_URL = os.environ.get("BINA_SEARCH_URL", (            # <-- EDIT
    "https://bina.az/baki/alqi-satqi/menziller?room_ids%5B%5D=2&room_ids%5B%5D=3"
    "&price_to=190000&area_from=55&has_bill_of_sale=true&has_mortgage=true"
    "&floor_first=false&floor_last=false&location_ids%5B%5D=8&location_ids%5B%5D=51"
    "&location_ids%5B%5D=2&location_ids%5B%5D=33&location_ids%5B%5D=54&location_ids%5B%5D=4"
    "&location_ids%5B%5D=52&location_ids%5B%5D=53&location_ids%5B%5D=1"
))

# The persisted-query hash bina.az's own website uses. If the bot ever tells you
# it changed, follow the "Refresh the hash" steps in the README and paste the new
# value here:
PERSISTED_HASH = os.environ.get("BINA_PERSISTED_HASH",           # <-- EDIT (only if asked)
    "872e9c694c34b6674514d48e9dcf1b46241d3d79f365ddf20d138f18e74554c5")

# ---------------------------------------------------------------------------
# Constants (no need to change)
# ---------------------------------------------------------------------------
GRAPHQL_URL = "https://bina.az/graphql"
OPERATION = "SearchItems"
SORT = "BUMPED_AT_DESC"          # site's "recent activity" order; new listings surface at the top
PAGE_SIZE = 16                   # API complexity cap; do not raise
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "6"))   # 6 pages = ~96 newest listings per run
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
MAX_SEEN = 6000                  # keep seen.json from growing forever
SEND_PHOTOS = os.environ.get("SEND_PHOTOS", "true").lower() == "true"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "az,en-US;q=0.9,en;q=0.8,ru;q=0.7",
    "Content-Type": "application/json",
    "Referer": "https://bina.az/alqi-satqi/menziller",
    "Origin": "https://bina.az",
}


def log(*args):
    print(*args, flush=True)


class PersistedQueryError(Exception):
    pass


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def tg_send_message(text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": False},
            timeout=30,
        )
        if r.status_code == 200:
            return True
        log("Telegram sendMessage failed:", r.status_code, r.text[:200])
        return False
    except requests.RequestException as e:
        log("Telegram sendMessage error:", e)
        return False


def tg_send_photo(photo_url: str, caption: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            json={"chat_id": CHAT_ID, "photo": photo_url, "caption": caption,
                  "parse_mode": "HTML"},
            timeout=30,
        )
        if r.status_code == 200:
            return True
        log("Telegram sendPhoto failed:", r.status_code, r.text[:200], "-> falling back to text")
        return tg_send_message(caption)
    except requests.RequestException as e:
        log("Telegram sendPhoto error:", e, "-> falling back to text")
        return tg_send_message(caption)


# ---------------------------------------------------------------------------
# Filter parsed from the bina.az search URL
# ---------------------------------------------------------------------------
def build_filter(url: str) -> dict:
    q = parse_qs(urlparse(url).query)

    def one(key):
        v = q.get(key)
        return v[0] if v else None

    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def as_bool(v):
        return None if v is None else str(v).lower() in ("1", "true", "yes", "on")

    return {
        "room_ids": {as_int(v) for v in q.get("room_ids[]", []) if as_int(v) is not None},
        "location_ids": {as_int(v) for v in q.get("location_ids[]", []) if as_int(v) is not None},
        "price_from": as_int(one("price_from")),
        "price_to": as_int(one("price_to")),
        "area_from": float(one("area_from")) if one("area_from") else None,
        "area_to": float(one("area_to")) if one("area_to") else None,
        "has_bill_of_sale": as_bool(one("has_bill_of_sale")),
        "has_mortgage": as_bool(one("has_mortgage")),
        "not_first_floor": as_bool(one("floor_first")) is True,
        "not_last_floor": as_bool(one("floor_last")) is True,
    }


def matches(listing: dict, f: dict) -> bool:
    if f["room_ids"] and listing.get("rooms") not in f["room_ids"]:
        return False
    price = listing.get("price")
    if f["price_to"] is not None and (price is None or price > f["price_to"]):
        return False
    if f["price_from"] is not None and (price is None or price < f["price_from"]):
        return False
    area = listing.get("area")
    if f["area_from"] is not None and (area is None or area < f["area_from"]):
        return False
    if f["area_to"] is not None and (area is None or area > f["area_to"]):
        return False
    if f["has_bill_of_sale"] is True and listing.get("has_bill_of_sale") is not True:
        return False
    if f["has_mortgage"] is True and listing.get("has_mortgage") is not True:
        return False
    if f["location_ids"]:
        lid = listing.get("location_id")
        if lid is None or int(lid) not in f["location_ids"]:
            return False
    floor, floors = listing.get("floor"), listing.get("floors")
    if f["not_first_floor"] and floor is not None and floor <= 1:
        return False
    if f["not_last_floor"] and floor is not None and floors is not None and floor >= floors:
        return False
    return True


# ---------------------------------------------------------------------------
# Bina.az GraphQL fetch
# ---------------------------------------------------------------------------
def _params(cursor):
    variables = {"first": PAGE_SIZE, "filter": {"leased": False}, "sort": SORT}
    if cursor:
        variables["cursor"] = cursor
    return {
        "operationName": OPERATION,
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(
            {"persistedQuery": {"version": 1, "sha256Hash": PERSISTED_HASH}},
            separators=(",", ":")),
    }


def _node_to_listing(node: dict) -> dict:
    def sub(key, field):
        obj = node.get(key)
        return obj.get(field) if isinstance(obj, dict) else None

    photos = node.get("photos") or []
    photo = None
    if photos and isinstance(photos[0], dict):
        photo = photos[0].get("large") or photos[0].get("f460x345") or photos[0].get("thumbnail")

    area = sub("area", "value")
    try:
        area = float(area) if area is not None else None
    except (TypeError, ValueError):
        area = None
    price = sub("price", "value")
    try:
        price = int(float(price)) if price is not None else None
    except (TypeError, ValueError):
        price = None

    path = node.get("path")
    return {
        "id": int(node["id"]),
        "rooms": node.get("rooms"),
        "area": area,
        "area_units": sub("area", "units") or "m²",
        "floor": node.get("floor"),
        "floors": node.get("floors"),
        "price": price,
        "currency": sub("price", "currency") or "AZN",
        "location_id": sub("location", "id"),
        "location": sub("location", "fullName") or sub("location", "name") or sub("city", "name"),
        "has_bill_of_sale": node.get("hasBillOfSale"),
        "has_mortgage": node.get("hasMortgage"),
        "has_repair": node.get("hasRepair"),
        "updated_at": node.get("updatedAt"),
        "url": f"https://bina.az{path}" if path else f"https://bina.az/items/{node['id']}",
        "photo": photo,
    }


def fetch_newest():
    """Return newest listings (newest first) across up to SCAN_PAGES pages."""
    out = []
    cursor = None
    for _ in range(SCAN_PAGES):
        r = requests.get(GRAPHQL_URL, params=_params(cursor), headers=HEADERS, timeout=30)
        if r.status_code != 200:
            log("bina.az HTTP", r.status_code, "- stopping this run")
            break
        payload = r.json()
        if payload.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in payload["errors"])
            if "PersistedQueryNotFound" in msg:
                raise PersistedQueryError(msg)
            log("GraphQL error:", msg)
            break
        conn = (payload.get("data") or {}).get("itemsConnection")
        if not conn:
            log("No itemsConnection in response")
            break
        for edge in conn.get("edges", []):
            node = edge.get("node")
            if node and node.get("id") is not None:
                try:
                    out.append(_node_to_listing(node))
                except Exception as e:
                    log("Skip bad node:", e)
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage") or not info.get("endCursor"):
            break
        cursor = info["endCursor"]
    return out


# ---------------------------------------------------------------------------
# State (seen.json)
# ---------------------------------------------------------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return None  # signals first run
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("listings", {})
        return data
    except Exception as e:
        log("Could not read state, starting fresh:", e)
        return {"listings": {}}


def save_state(state):
    listings = state["listings"]
    if len(listings) > MAX_SEEN:  # keep the newest MAX_SEEN by first_seen
        kept = sorted(listings.items(), key=lambda kv: kv[1].get("first_seen", ""), reverse=True)[:MAX_SEEN]
        state["listings"] = dict(kept)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------
def _fmt_published(iso):
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(str(iso)).strftime("%d %b %Y, %H:%M")
    except Exception:
        return str(iso)


def format_message(l: dict) -> str:
    lines = ["🏠 <b>NEW APARTMENT FOUND</b>", ""]
    if l.get("price") is not None:
        lines.append(f"💰 <b>Price:</b> {l['price']:,} {l['currency']}")
    lines.append(f"🛏 <b>Rooms:</b> {l.get('rooms') if l.get('rooms') is not None else '-'}")
    if l.get("area") is not None:
        area = int(l["area"]) if float(l["area"]).is_integer() else l["area"]
        lines.append(f"📐 <b>Area:</b> {area} {l['area_units']}")
    floor = f"{l['floor']}/{l['floors']}" if l.get("floor") and l.get("floors") else (str(l.get("floor")) if l.get("floor") else "-")
    lines.append(f"🏢 <b>Floor:</b> {floor}")
    if l.get("location"):
        lines.append(f"📍 <b>Location:</b> {html.escape(str(l['location']))}")
    pub = _fmt_published(l.get("updated_at"))
    if pub:
        lines.append(f"📅 <b>Published:</b> {html.escape(pub)}")
    tags = []
    if l.get("has_bill_of_sale"):
        tags.append("kupçalı")
    if l.get("has_mortgage"):
        tags.append("ipoteka")
    if l.get("has_repair"):
        tags.append("təmirli")
    if tags:
        lines.append("✅ " + ", ".join(tags))
    lines.append("")
    lines.append(f'🔗 <a href="{html.escape(l["url"])}">Open listing</a>')
    return "\n".join(lines)


def notify(l: dict) -> bool:
    text = format_message(l)
    if SEND_PHOTOS and l.get("photo"):
        return tg_send_photo(l["photo"], text)
    return tg_send_message(text)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not BOT_TOKEN or not CHAT_ID:
        log("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set as secrets.")
        sys.exit(1)

    state = load_state()
    first_run = state is None
    if first_run:
        state = {"listings": {}}
    seen = state["listings"]

    try:
        newest = fetch_newest()
    except PersistedQueryError:
        log("Persisted query hash changed.")
        tg_send_message(
            "⚠️ <b>bina.az updated its site.</b> The request signature (persisted "
            "query hash) changed, so monitoring is paused until you refresh it.\n\n"
            "See the README section <i>“Refresh the hash”</i> — it is a 2-minute "
            "copy-paste. After updating monitor.py, monitoring resumes automatically."
        )
        return  # do not touch state

    if not newest:
        log("No listings fetched this run (transient). Will retry next run.")
        return

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    new_matches, seeded = [], 0

    for l in newest:
        idk = str(l["id"])
        if idk in seen:
            continue
        if matches(l, build_filter(BINA_SEARCH_URL)):
            if first_run:
                seen[idk] = {"url": l["url"], "price": l["price"], "first_seen": now,
                             "notification_sent": True, "matched": True}
                seeded += 1
            else:
                new_matches.append(l)
        else:
            # Record non-matches too, so a later "bump" of an old listing can't
            # masquerade as new.
            seen[idk] = {"first_seen": now, "notification_sent": True, "matched": False}

    notified = 0
    if first_run:
        log(f"First run: seeded {seeded} current matching listings (no spam).")
        tg_send_message(
            f"✅ <b>Monitoring started.</b>\nRecorded {seeded} current matching "
            f"listing(s). From now on you'll only get brand-new ones.\n\n"
            f"Scanned {len(newest)} newest listings this run."
        )
    else:
        for l in new_matches:
            if notify(l):
                seen[str(l["id"])] = {"url": l["url"], "price": l["price"],
                                      "first_seen": now, "notification_sent": True, "matched": True}
                notified += 1
                log("Notified:", l["id"], l.get("price"), l.get("location"))
            else:
                log("Send failed, will retry next run:", l["id"])

    save_state(state)
    log(f"Done. scanned={len(newest)} new_matches={len(new_matches)} notified={notified} "
        f"seen_total={len(seen)}")


if __name__ == "__main__":
    main()
