#!/usr/bin/env python3
"""
VIBO-Wohnungsbot (nur neue Wohnungen)
Prüft https://vibo.dk/boligsoegende/ledige-boliger/ und schickt eine
Push-Nachricht ausschließlich dann, wenn neue Wohnungen hinzugekommen sind.

Nur Python-Standardbibliothek, keine Installation nötig.

Umgebungsvariablen:
  NTFY_TOPIC          Name deines ntfy-Themas
  NTFY_SERVER         optional, Standard https://ntfy.sh
  TELEGRAM_BOT_TOKEN  optional
  TELEGRAM_CHAT_ID    optional
  STATE_FILE          optional, Standard state.json
"""

import base64
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

URL = "https://vibo.dk/boligsoegende/ledige-boliger/"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
UA = "Mozilla/5.0 (compatible; vibo-watcher/2.0; private notification bot)"

ID_RE = re.compile(r"\b(\d{3}-\d{3,4})\b")
MONEY = r"([\d.]+,\d{2})"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "da,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_tags(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|h\d|tr|div)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_listings(page: str) -> list[dict]:
    listings, seen = [], set()
    for block in re.findall(r"<details\b.*?</details>", page, flags=re.S | re.I):
        m = re.search(r'class="accordion-title"[^>]*>(.*?)</div>', block, flags=re.S)
        title = one_line(strip_tags(m.group(1))) if m else ""
        if not title:
            continue
        body_m = re.search(r'class="accordion-content[^"]*"[^>]*>(.*)</div>', block, flags=re.S)
        body = strip_tags(body_m.group(1)) if body_m else strip_tags(block)

        idm = ID_RE.search(title) or ID_RE.search(body)
        lid = idm.group(1) if idm else "h-" + hashlib.sha1(title.encode()).hexdigest()[:10]
        if lid in seen:
            lid += "-" + hashlib.sha1(title.encode()).hexdigest()[:6]
        seen.add(lid)

        def grab(pattern):
            mm = re.search(pattern, body, flags=re.I)
            return one_line(mm.group(1)) if mm else None

        listings.append({
            "id": lid,
            "title": title,
            "size": grab(r"Størrelse:\s*([^\n]+)"),
            "address": grab(r"Adresse:\s*([^\n]+)"),
            "period": grab(r"Lejeperiode:\s*([^\n]+)"),
            "rent": grab(r"Husleje kr\.?\s*" + MONEY),
            "total": grab(r"I alt\s*kr\.?\s*" + MONEY),
        })
    return listings


def describe(l: dict) -> str:
    lines = [l["address"] or l["title"]]
    facts = [x for x in (
        l.get("size"),
        f"{l['total']} kr./Monat gesamt" if l.get("total") else (f"Miete {l['rent']} kr." if l.get("rent") else None),
    ) if x]
    if facts:
        lines.append(" · ".join(facts))
    if l.get("period"):
        lines.append("Zeitraum: " + l["period"])
    lines.append(f"Bolignr. {l['id']}")
    return "\n".join(lines)


def notify(title: str, message: str) -> None:
    sent = False
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        req = urllib.request.Request(
            f"{server}/{urllib.parse.quote(topic)}",
            data=message.encode("utf-8"),
            headers={
                "Title": "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?=",
                "Priority": "high",
                "Tags": "house",
                "Click": URL,
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=30).read()
        sent = True

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        data = urllib.parse.urlencode({
            "chat_id": chat,
            "text": f"{title}\n\n{message}\n\n{URL}",
            "disable_web_page_preview": "true",
        }).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30).read()
        sent = True

    if not sent:
        print("[kein Benachrichtigungskanal konfiguriert]")
    print(f"--- {title}\n{message}\n")


def load_ids() -> set[str] | None:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    # kompatibel mit dem Stand des alten Bots ({"listings": {id: ...}})
    if isinstance(data, dict) and "listings" in data:
        return set(data["listings"])
    return set(data.get("ids", []))


def save_ids(ids: set[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": sorted(ids)}, f, indent=2)
        f.write("\n")


def main() -> int:
    page = fetch(URL)
    current = parse_listings(page)
    if not current and "Ledige boliger" not in page:
        print("Unbekannte Seitenstruktur – Stand wird nicht verändert.")
        return 1

    known = load_ids()
    current_ids = {l["id"] for l in current}
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if known is None:
        # Erster Lauf: nur merken, keine Nachricht
        print(f"{stamp}: Erster Lauf, {len(current)} Anzeigen gemerkt.")
        save_ids(current_ids)
        return 0

    new = [l for l in current if l["id"] not in known]
    if new:
        head = "Neue VIBO-Wohnung!" if len(new) == 1 else f"{len(new)} neue VIBO-Wohnungen!"
        notify(head, "\n\n".join(describe(l) for l in new))

    print(f"{stamp}: {len(current)} Anzeigen, {len(new)} neu.")
    # Bekannte IDs behalten, damit eine kurz verschwundene Anzeige nicht erneut gemeldet wird
    save_ids(known | current_ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
