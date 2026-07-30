"""Fetch today's MLB, NBA, and World Cup games and send a daily email."""

from __future__ import annotations

import html
import os
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
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}

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


def fetch_nba(date: str) -> List[Game]:
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
    title_zh: str
    url: str
    source: str
    published: str


def translate_zh(text: str) -> str:
    try:
        q = urllib.parse.quote(text)
        r = requests.get(
            f"https://api.mymemory.translated.net/get?q={q}&langpair=en|zh-CN",
            timeout=10,
            headers=HEADERS,
        )
        data = r.json()
        return data["responseData"]["translatedText"] or text
    except Exception:
        return text


def fetch_news() -> List[NewsItem]:
    seen_urls: set[str] = set()
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
                seen_urls.add(link)
                try:
                    dt = parsedate_to_datetime(pub)
                except Exception:
                    dt = datetime.min.replace(tzinfo=ZoneInfo("UTC"))
                clean_title = title.rsplit(" - ", 1)[0].strip() if " - " in title else title
                title_zh = translate_zh(clean_title)
                items.append((dt, {"title": clean_title, "title_zh": title_zh, "url": link, "source": source, "published": pub}))
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


def no_game_msg(title: str, date: str) -> str:
    month = int(date[5:7])
    if title == "NBA" and month in (7, 8, 9):
        return "NBA season has ended. See you next season! 🏀"
    return NO_GAME


def text_section(title: str, games: List[Game] | str, date: str = "") -> str:
    if isinstance(games, str):
        return f"{title}\n  {games}"
    if not games:
        return f"{title}\n  {no_game_msg(title, date)}"
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
    lines = [f"  • {n['title']}\n    {n['title_zh']} ({n['source']})\n    {n['url']}" for n in items]
    return "Shohei News\n" + "\n".join(lines)


def news_html(items: List[NewsItem]) -> str:
    header = (
        '<style>'
        '@keyframes shohei-pitch{'
        '0%{transform:rotate(-20deg) translateY(0)}'
        '30%{transform:rotate(15deg) translateY(-4px)}'
        '60%{transform:rotate(-10deg) translateY(2px)}'
        '100%{transform:rotate(-20deg) translateY(0)}'
        '}'
        '.shohei-char{display:inline-block;animation:shohei-pitch 1.8s ease-in-out infinite;'
        'font-size:26px;line-height:1;vertical-align:middle;margin-left:10px;}'
        '</style>'
        '<h2 style="margin:28px 0 14px;padding:10px 14px;font:700 22px/1.3 -apple-system,'
        'Segoe UI,Roboto,sans-serif;color:#fff;background:#1a3a5c;border-radius:6px;'
        'display:flex;align-items:center;justify-content:space-between;">'
        'Shohei News'
        '<span class="shohei-char">⚾</span>'
        '</h2>'
    )
    if not items:
        return header + '<p style="margin:0;padding:12px 14px;color:#666;font:italic 16px/1.5 -apple-system,sans-serif;">No news found today.</p>'
    rows = []
    for n in items:
        rows.append(
            f'<tr><td style="padding:12px 14px;border-bottom:1px solid #eee;">'
            f'<a href="{html.escape(n["url"])}" style="color:#2c5f8a;font:600 16px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;text-decoration:none;">'
            f'{html.escape(n["title"])}</a>'
            f'<div style="color:#555;font-size:14px;margin-top:4px;">{html.escape(n["title_zh"])}</div>'
            f'<div style="color:#aaa;font-size:12px;margin-top:2px;">{html.escape(n["source"])}</div>'
            f'</td></tr>'
        )
    table = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;border:1px solid #eee;border-radius:6px;overflow:hidden;">'
        + "".join(rows) + "</table>"
    )
    return header + table


def build_text(date: str, sections: list[tuple[str, List[Game] | str]], news: List[NewsItem]) -> str:
    parts = [f"Today's Games — {date}"]
    parts.extend(text_section(t, g, date) for t, g in sections)
    parts.append(news_text(news))
    return "\n\n".join(parts)


def html_section(title: str, games: List[Game] | str, date: str = "") -> str:
    color = SPORT_COLORS.get(title, "#333")
    header = (
        f'<h2 style="margin:28px 0 14px;padding:10px 14px;font:700 22px/1.3 -apple-system,'
        f'Segoe UI,Roboto,sans-serif;color:#fff;background:{color};border-radius:6px;">'
        f"{html.escape(title)}</h2>"
    )
    if isinstance(games, str):
        return header + f'<p style="margin:0;padding:12px 14px;color:#b00;font:16px/1.5 -apple-system,sans-serif;">{html.escape(games)}</p>'
    if not games:
        msg = no_game_msg(title, date)
        return header + f'<p style="margin:0;padding:12px 14px;color:#666;font:italic 16px/1.5 -apple-system,sans-serif;">{html.escape(msg)}</p>'
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


def build_html(date: str, sections: list[tuple[str, List[Game] | str]], news: List[NewsItem]) -> str:
    body = "".join(html_section(t, g, date) for t, g in sections)
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
        f"{news_html(news)}"
        '<p style="margin:24px 0 0;padding-top:16px;border-top:1px solid #eee;'
        'color:#999;font:13px/1.4 -apple-system,sans-serif;">'
        "Sent daily at 12:00 PM Toronto time.</p>"
        "</td></tr></table></td></tr></table></body></html>"
    )


def send_email(text_body: str, html_body: str, date: str) -> None:
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

    with smtplib.SMTP(host, port) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, password)
        s.send_message(msg)


def main() -> None:
    tz = ZoneInfo("America/Toronto")
    now = datetime.now(tz)
    date = now.strftime("%Y-%m-%d")
    sections = [
        ("MLB", safe_fetch(fetch_mlb, date)),
        ("NBA", safe_fetch(fetch_nba, date)),
    ]
    news = fetch_news()
    text_body = build_text(date, sections, news)
    html_body = build_html(date, sections, news)

    if os.environ.get("DRY_RUN") == "1":
        out = os.environ.get("DRY_RUN_FORMAT", "text")
        print(html_body if out == "html" else text_body)
        return
    send_email(text_body, html_body, date)


if __name__ == "__main__":
    main()
