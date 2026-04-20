"""Fetch today's MLB, NBA, and World Cup games and send a daily email."""

from __future__ import annotations

import html
import os
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from typing import Callable, List, TypedDict
from zoneinfo import ZoneInfo

import requests

TIMEOUT = 15
FEATURED_MLB_TEAMS = {"Toronto Blue Jays", "Los Angeles Dodgers"}
NO_GAME = "No game happens today"

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


def make_game(away: str, home: str, time: str, separator: str = "@", featured: bool = False) -> Game:
    return {"away": away, "home": home, "time": time, "separator": separator, "featured": featured}


def fetch_mlb(date: str) -> List[Game]:
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}"
    data = requests.get(url, timeout=TIMEOUT).json()
    games: List[Game] = []
    for day in data.get("dates", []):
        for g in day.get("games", []):
            home = g["teams"]["home"]["team"]["name"]
            away = g["teams"]["away"]["team"]["name"]
            featured = home in FEATURED_MLB_TEAMS or away in FEATURED_MLB_TEAMS
            games.append(make_game(away, home, format_time(g.get("gameDate")), "@", featured))
    games.sort(key=lambda x: 0 if x["featured"] else 1)
    return games


def fetch_nba(date: str) -> List[Game]:
    url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
    data = requests.get(url, timeout=TIMEOUT).json()
    target = date.replace("-", "")
    games: List[Game] = []
    for gd in data.get("leagueSchedule", {}).get("gameDates", []):
        gd_date = datetime.strptime(gd["gameDate"], "%m/%d/%Y %H:%M:%S").strftime("%Y%m%d")
        if gd_date != target:
            continue
        for g in gd.get("games", []):
            away = f"{g['awayTeam']['teamCity']} {g['awayTeam']['teamName']}".strip()
            home = f"{g['homeTeam']['teamCity']} {g['homeTeam']['teamName']}".strip()
            games.append(make_game(away, home, format_time(g.get("gameDateTimeUTC")), "@"))
    return games


def fetch_world_cup(date: str) -> List[Game]:
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
        f"?dates={date.replace('-', '')}"
    )
    data = requests.get(url, timeout=TIMEOUT).json()
    games: List[Game] = []
    for event in data.get("events", []):
        comps = event.get("competitions", [{}])[0].get("competitors", [])
        if len(comps) < 2:
            continue
        home = next((c["team"]["displayName"] for c in comps if c.get("homeAway") == "home"), "")
        away = next((c["team"]["displayName"] for c in comps if c.get("homeAway") == "away"), "")
        games.append(make_game(away, home, format_time(event.get("date")), "vs"))
    return games


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


def text_section(title: str, games: List[Game] | str) -> str:
    if isinstance(games, str):
        return f"{title}\n  {games}"
    if not games:
        return f"{title}\n  {NO_GAME}"
    lines = []
    for g in games:
        tag = " [FEATURED]" if g["featured"] else ""
        lines.append(f"  • {g['away']} {g['separator']} {g['home']} — {g['time']}{tag}")
    return f"{title}\n" + "\n".join(lines)


def build_text(date: str, sections: list[tuple[str, List[Game] | str]]) -> str:
    parts = [f"Today's Games — {date}"]
    parts.extend(text_section(t, g) for t, g in sections)
    return "\n\n".join(parts)


def html_section(title: str, games: List[Game] | str) -> str:
    color = SPORT_COLORS.get(title, "#333")
    header = (
        f'<h2 style="margin:24px 0 12px;padding:8px 12px;font:600 18px/1.3 -apple-system,'
        f'Segoe UI,Roboto,sans-serif;color:#fff;background:{color};border-radius:6px;">'
        f"{html.escape(title)}</h2>"
    )
    if isinstance(games, str):
        return header + f'<p style="margin:0;color:#b00;font:14px/1.5 -apple-system,sans-serif;">{html.escape(games)}</p>'
    if not games:
        return header + f'<p style="margin:0;color:#666;font:italic 14px/1.5 -apple-system,sans-serif;">{NO_GAME}</p>'
    rows = []
    for g in games:
        badge = ""
        bg = "#ffffff"
        if g["featured"]:
            badge = (
                '<span style="display:inline-block;margin-left:8px;padding:2px 8px;'
                'background:#ffd60a;color:#000;border-radius:10px;font:600 11px/1 sans-serif;">'
                "FEATURED</span>"
            )
            bg = "#fffbea"
        rows.append(
            f'<tr><td style="padding:10px 12px;background:{bg};border-bottom:1px solid #eee;'
            'font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;color:#222;">'
            f'<strong>{html.escape(g["away"])}</strong> '
            f'<span style="color:#888;">{html.escape(g["separator"])}</span> '
            f'<strong>{html.escape(g["home"])}</strong>{badge}'
            f'<div style="color:#666;font-size:12px;margin-top:2px;">{html.escape(g["time"])}</div>'
            "</td></tr>"
        )
    table = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;border:1px solid #eee;border-radius:6px;overflow:hidden;">'
        + "".join(rows) + "</table>"
    )
    return header + table


def build_html(date: str, sections: list[tuple[str, List[Game] | str]]) -> str:
    body = "".join(html_section(t, g) for t, g in sections)
    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:#f5f5f7;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f7;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="max-width:600px;width:100%;background:#fff;border-radius:10px;'
        'box-shadow:0 1px 3px rgba(0,0,0,0.06);padding:24px;">'
        '<tr><td>'
        '<h1 style="margin:0 0 4px;font:700 22px/1.2 -apple-system,Segoe UI,Roboto,sans-serif;color:#111;">'
        "Today's Games</h1>"
        f'<p style="margin:0;color:#666;font:14px/1.4 -apple-system,sans-serif;">{html.escape(date)}</p>'
        f"{body}"
        '<p style="margin:24px 0 0;padding-top:16px;border-top:1px solid #eee;'
        'color:#999;font:12px/1.4 -apple-system,sans-serif;">'
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
        ("World Cup", safe_fetch(fetch_world_cup, date)),
    ]
    text_body = build_text(date, sections)
    html_body = build_html(date, sections)

    if os.environ.get("DRY_RUN") == "1":
        out = os.environ.get("DRY_RUN_FORMAT", "text")
        print(html_body if out == "html" else text_body)
        return
    if os.environ.get("ENFORCE_NOON") == "1" and now.hour != 12:
        print(f"Skipping: local hour is {now.hour}, not 12.")
        return
    send_email(text_body, html_body, date)


if __name__ == "__main__":
    main()
