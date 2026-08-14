"""Fetch today's MLB, NBA, and World Cup games and send a daily email."""

from __future__ import annotations

import html
import os
import random
import smtplib
import ssl
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Callable, List, TypedDict
from zoneinfo import ZoneInfo

import requests

TIMEOUT = 15
FEATURED_MLB_TEAMS = {"Toronto Blue Jays", "Los Angeles Dodgers"}
NO_GAME = "No game happens today"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.espn.com/",
    "Origin": "https://www.espn.com",
}

SPORT_COLORS = {
    "MLB": "#D50032",
    "NBA": "#1D428A",
    "World Cup": "#326295",
}


class Game(TypedDict):
    away: str
    home: str
    time: str
    separator: str
    featured: bool
    series: str


def make_game(away: str, home: str, time: str, separator: str = "@", featured: bool = False, series: str = "") -> Game:
    return {"away": away, "home": home, "time": time, "separator": separator, "featured": featured, "series": series}


def _get_json(url: str) -> dict:
    r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        snippet = r.text[:200].replace("\n", " ")
        raise RuntimeError(f"non-JSON response (HTTP {r.status_code}): {snippet!r}")


def fetch_mlb(date: str) -> List[Game]:
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}&hydrate=seriesStatus"
    data = _get_json(url)
    games: List[Game] = []
    for day in data.get("dates", []):
        for g in day.get("games", []):
            home = g["teams"]["home"]["team"]["name"]
            away = g["teams"]["away"]["team"]["name"]
            if home not in FEATURED_MLB_TEAMS and away not in FEATURED_MLB_TEAMS:
                continue
            featured = True
            series = ""
            if g.get("gameType", "R") != "R":
                status = g.get("seriesStatus") or {}
                label = g.get("seriesDescription") or ""
                score = status.get("shortDescription") or status.get("description") or ""
                series = " · ".join(x for x in (label, score) if x)
            games.append(make_game(away, home, format_time(g.get("gameDate")), "@", featured, series))
    return games


def _fetch_nba_espn(date: str) -> List[Game]:
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
        f"?dates={date.replace('-', '')}"
    )
    data = _get_json(url)
    games: List[Game] = []
    for event in data.get("events", []):
        competition = event.get("competitions", [{}])[0]
        comps = competition.get("competitors", [])
        if len(comps) < 2:
            continue
        home = next((c["team"]["displayName"] for c in comps if c.get("homeAway") == "home"), "")
        away = next((c["team"]["displayName"] for c in comps if c.get("homeAway") == "away"), "")
        notes = competition.get("notes") or []
        series = notes[0].get("headline", "") if notes else ""
        games.append(make_game(away, home, format_time(event.get("date")), "@", series=series))
    return games


def _fetch_nba_cdn(date: str) -> List[Game]:
    url = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
    data = _get_json(url)
    games: List[Game] = []
    for g in data.get("scoreboard", {}).get("games", []):
        home = g.get("homeTeam", {})
        away = g.get("awayTeam", {})
        home_name = f"{home.get('teamCity', '')} {home.get('teamName', '')}".strip()
        away_name = f"{away.get('teamCity', '')} {away.get('teamName', '')}".strip()
        series = g.get("seriesText", "") or ""
        games.append(make_game(away_name, home_name, format_time(g.get("gameTimeUTC")), "@", series=series))
    return games


def fetch_nba(date: str) -> List[Game]:
    try:
        return _fetch_nba_espn(date)
    except Exception:
        return _fetch_nba_cdn(date)


def fetch_world_cup(date: str) -> List[Game]:
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
        f"?dates={date.replace('-', '')}"
    )
    data = _get_json(url)
    games: List[Game] = []
    for event in data.get("events", []):
        competition = event.get("competitions", [{}])[0]
        comps = competition.get("competitors", [])
        if len(comps) < 2:
            continue
        home = next((c["team"]["displayName"] for c in comps if c.get("homeAway") == "home"), "")
        away = next((c["team"]["displayName"] for c in comps if c.get("homeAway") == "away"), "")
        notes = competition.get("notes") or []
        stage = notes[0].get("headline", "") if notes else ""
        games.append(make_game(away, home, format_time(event.get("date")), "vs", series=stage))
    return games


NEWS_TOPICS = ["Shohei Ohtani"]
NEWS_COUNT = 3


class NewsItem(TypedDict):
    title: str
    url: str
    source: str
    published: str


def fetch_news() -> List[NewsItem]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    items: List[tuple[datetime, NewsItem]] = []
    for topic in NEWS_TOPICS:
        q = urllib.parse.quote(topic)
        url = f"https://news.google.com/rss/search?q={q}&hl=en-CA&gl=CA&ceid=CA:en"
        try:
            r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for item in root.findall("./channel/item"):
                link = (item.findtext("link") or "").strip()
                title = (item.findtext("title") or "").strip()
                pub = (item.findtext("pubDate") or "").strip()
                source_el = item.find("source")
                source = source_el.text.strip() if source_el is not None and source_el.text else ""
                if not link or link in seen_urls:
                    continue
                clean_title = title.rsplit(" - ", 1)[0].strip() if " - " in title else title
                title_key = clean_title.strip().lower()
                if title_key in seen_titles:
                    continue
                seen_urls.add(link)
                seen_titles.add(title_key)
                try:
                    dt = parsedate_to_datetime(pub)
                except Exception:
                    dt = datetime.min.replace(tzinfo=ZoneInfo("UTC"))
                items.append((dt, {"title": clean_title, "url": link, "source": source, "published": pub}))
        except Exception:
            continue
    items.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in items[:NEWS_COUNT]]


def format_time(iso: str | None) -> str:
    if not iso:
        return "TBD"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Toronto"))
    return dt.strftime("%-I:%M %p ET")


def safe_fetch(fn: Callable[[str], List[Game]], date: str) -> List[Game] | str:
    try:
        return fn(date)
    except Exception as e:
        return f"(error fetching: {e})"


def is_nba_offseason(date: str) -> bool:
    month = int(date[5:7])
    return month in (7, 8, 9)


def text_section(title: str, games: List[Game] | str) -> str:
    if isinstance(games, str):
        return f"{title}\n  {games}"
    if not games:
        return f"{title}\n  {NO_GAME}"
    lines = []
    for g in games:
        base = f"  • {g['away']} {g['separator']} {g['home']} — {g['time']}"
        if g["series"]:
            base += f"\n      ({g['series']})"
        lines.append(base)
    return f"{title}\n" + "\n".join(lines)


def news_text(items: List[NewsItem]) -> str:
    if not items:
        return "Shohei News\n  No news found today."
    lines = [f"  • {n['title']} ({n['source']})\n    {n['url']}" for n in items]
    return "Shohei News\n" + "\n".join(lines)


def _commons_photo(filename: str, width: int = 240) -> str:
    """Wikimedia Commons' officially documented stable hotlink redirect —
    resolves to the current upload.wikimedia.org thumb URL server-side,
    so we never have to hand-guess the internal MD5 hash path itself."""
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{urllib.parse.quote(filename)}?width={width}"


# All sourced from Wikimedia Commons' Category:Shohei_Ohtani — Commons only
# hosts public-domain or Creative-Commons-licensed media by its own policy.
SHOHEI_PHOTOS = [
    _commons_photo("Shohei_Ohtani_in_2015.jpg"),  # CC0 / public domain
    _commons_photo("Fighters_ohtani_11.jpg"),  # CC-BY-SA-3.0
    _commons_photo("Shohei_Ohtani_2023_WBC.jpg"),  # CC-BY-SA
    _commons_photo("Shohei_Ohtani_-_Los_Angeles_Dodgers_(2024).jpg"),  # CC-BY-SA
    _commons_photo("Dodgers_at_Nationals_(53677192000)_(cropped).jpg"),  # Flickr-import, CC-BY/CC-BY-SA
    _commons_photo("Shohei_Ohtani_on_April_23,_2024_(2)_53677091634.jpg"),  # Flickr-import, CC-BY/CC-BY-SA
]


def pick_shohei_photo(date: str) -> str:
    return random.Random(date).choice(SHOHEI_PHOTOS)


SHOHEI_PHOTO_CID = "shohei_photo"


def fetch_shohei_photo(date: str) -> tuple[bytes, str] | None:
    """Fetch the day's Shohei photo bytes server-side, trying every pool
    photo (starting from the day's pick) until one succeeds, so a broken
    or hotlink-blocked source doesn't break the embedded image."""
    first = pick_shohei_photo(date)
    ordered = [first] + [u for u in SHOHEI_PHOTOS if u != first]
    for url in ordered:
        try:
            r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
            r.raise_for_status()
            content_type = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            subtype = content_type.split("/", 1)[1] if "/" in content_type else "jpeg"
            return r.content, subtype
        except Exception:
            continue
    return None


def news_html(items: List[NewsItem], has_photo: bool = True) -> str:
    if has_photo:
        polaroid = (
            '<div style="display:inline-block;background:#fff;padding:4px 4px 14px 4px;'
            'box-shadow:2px 3px 8px rgba(0,0,0,0.45);transform:rotate(4deg);'
            'border-radius:2px;vertical-align:middle;margin-left:10px;flex-shrink:0;">'
            f'<img src="cid:{SHOHEI_PHOTO_CID}" '
            'width="55" height="55" style="display:block;object-fit:cover;" alt="Shohei"/>'
            '</div>'
        )
    else:
        polaroid = ""
    header = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:28px 0 14px;background:#1a3a5c;border-radius:6px;overflow:hidden;">'
        '<tr>'
        '<td style="padding:10px 14px;font:700 22px/1.3 -apple-system,Segoe UI,Roboto,sans-serif;color:#fff;vertical-align:middle;">'
        'Shohei News'
        '</td>'
        f'<td style="padding:8px 12px 8px 0;text-align:right;vertical-align:middle;width:75px;">'
        f'{polaroid}'
        '</td>'
        '</tr>'
        '</table>'
    )
    if not items:
        return header + '<p style="margin:0;padding:12px 14px;color:#666;font:italic 16px/1.5 -apple-system,sans-serif;">No news found today.</p>'
    rows = []
    for n in items:
        rows.append(
            f'<tr><td style="padding:12px 14px;border-bottom:1px solid #eee;">'
            f'<a href="{html.escape(n["url"])}" style="color:#2c5f8a;font:600 16px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;text-decoration:none;">'
            f'{html.escape(n["title"])}</a>'
            f'<div style="color:#aaa;font-size:12px;margin-top:2px;">{html.escape(n["source"])}</div>'
            f'</td></tr>'
        )
    table = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;border:1px solid #eee;border-radius:6px;overflow:hidden;">'
        + "".join(rows) + "</table>"
    )
    return header + table


def build_text(date: str, mlb: List[Game] | str, nba: List[Game] | str | None, news: List[NewsItem]) -> str:
    parts = [f"Today's Games — {date}", text_section("MLB", mlb), news_text(news)]
    if nba is not None:
        parts.append(text_section("NBA", nba))
    return "\n\n".join(parts)


def html_section(title: str, games: List[Game] | str) -> str:
    color = SPORT_COLORS.get(title, "#333")
    header = (
        f'<h2 style="margin:28px 0 14px;padding:10px 14px;font:700 22px/1.3 -apple-system,'
        f'Segoe UI,Roboto,sans-serif;color:#fff;background:{color};border-radius:6px;">'
        f"{html.escape(title)}</h2>"
    )
    if isinstance(games, str):
        return header + f'<p style="margin:0;padding:12px 14px;color:#b00;font:16px/1.5 -apple-system,sans-serif;">{html.escape(games)}</p>'
    if not games:
        return header + f'<p style="margin:0;padding:12px 14px;color:#666;font:italic 16px/1.5 -apple-system,sans-serif;">{html.escape(NO_GAME)}</p>'
    rows = []
    for g in games:
        bg = "#ffffff"
        weight = "400"
        series_html = ""
        if g["series"]:
            series_html = (
                f'<div style="color:#666;font-size:14px;margin-top:4px;">'
                f'{html.escape(g["series"])}</div>'
            )
        rows.append(
            f'<tr><td style="padding:12px 14px;background:{bg};border-bottom:1px solid #eee;'
            f'font:{weight} 17px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;color:#222;">'
            f'{html.escape(g["away"])} '
            f'<span style="color:#888;">{html.escape(g["separator"])}</span> '
            f'{html.escape(g["home"])}'
            f'<div style="color:#444;font-size:18px;font-weight:600;margin-top:6px;">{html.escape(g["time"])}</div>'
            f'{series_html}'
            "</td></tr>"
        )
    table = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;border:1px solid #eee;border-radius:6px;overflow:hidden;">'
        + "".join(rows) + "</table>"
    )
    return header + table


def build_html(date: str, mlb: List[Game] | str, nba: List[Game] | str | None, news: List[NewsItem], has_photo: bool = True) -> str:
    body = html_section("MLB", mlb) + news_html(news, has_photo)
    if nba is not None:
        body += html_section("NBA", nba)
    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:#f5f5f7;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f7;">'
        '<tr><td align="center" style="padding:16px 4px;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="max-width:600px;width:100%;background:#fff;border-radius:10px;'
        'box-shadow:0 1px 3px rgba(0,0,0,0.06);padding:18px 14px;">'
        '<tr><td>'
        '<h1 style="margin:0 0 6px;font:700 26px/1.2 -apple-system,Segoe UI,Roboto,sans-serif;color:#111;">'
        "Today's Games</h1>"
        f'<p style="margin:0;color:#666;font:16px/1.4 -apple-system,sans-serif;">{html.escape(date)}</p>'
        f"{body}"
        '<p style="margin:24px 0 0;padding-top:16px;border-top:1px solid #eee;'
        'color:#999;font:13px/1.4 -apple-system,sans-serif;">'
        "Sent daily at 12:00 PM Toronto time.</p>"
        "</td></tr></table></td></tr></table></body></html>"
    )


def send_email(text_body: str, html_body: str, date: str, photo: tuple[bytes, str] | None) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("EMAIL_FROM", user)
    recipients = [r.strip() for r in os.environ["EMAIL_TO"].split(",") if r.strip()]

    msg = EmailMessage()
    msg["Subject"] = f"Today's Games — {date}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if photo:
        photo_bytes, subtype = photo
        html_part = msg.get_payload()[1]
        html_part.add_related(photo_bytes, maintype="image", subtype=subtype, cid=f"<{SHOHEI_PHOTO_CID}>")

    with smtplib.SMTP(host, port) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, password)
        s.send_message(msg)


def main() -> None:
    tz = ZoneInfo("America/Toronto")
    now = datetime.now(tz)
    date = now.strftime("%Y-%m-%d")
    mlb_games = safe_fetch(fetch_mlb, date)
    nba_games = None if is_nba_offseason(date) else safe_fetch(fetch_nba, date)
    news = fetch_news()
    photo = fetch_shohei_photo(date)
    text_body = build_text(date, mlb_games, nba_games, news)
    html_body = build_html(date, mlb_games, nba_games, news, has_photo=photo is not None)

    if os.environ.get("DRY_RUN") == "1":
        out = os.environ.get("DRY_RUN_FORMAT", "text")
        print(html_body if out == "html" else text_body)
        return
    send_email(text_body, html_body, date, photo)


if __name__ == "__main__":
    main()
