"""Fetch today's MLB, NBA, and World Cup games and send a daily email."""

from __future__ import annotations

import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import List
from zoneinfo import ZoneInfo

import requests

TIMEOUT = 15
FEATURED_MLB_TEAMS = {"Toronto Blue Jays", "Los Angeles Dodgers"}
NO_GAME = "No game happens today"


def today_in(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")


def fetch_mlb(date: str) -> List[str]:
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}"
    data = requests.get(url, timeout=TIMEOUT).json()
    games = []
    for day in data.get("dates", []):
        for g in day.get("games", []):
            home = g["teams"]["home"]["team"]["name"]
            away = g["teams"]["away"]["team"]["name"]
            start = format_time(g.get("gameDate"))
            games.append((away, home, start))

    def featured_first(entry):
        away, home, _ = entry
        return 0 if (away in FEATURED_MLB_TEAMS or home in FEATURED_MLB_TEAMS) else 1

    games.sort(key=featured_first)
    return [f"{a} @ {h} — {t}" for a, h, t in games]


def fetch_nba(date: str) -> List[str]:
    url = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
    data = requests.get(url, timeout=TIMEOUT).json()
    target = date.replace("-", "")
    games = []
    for gd in data.get("leagueSchedule", {}).get("gameDates", []):
        gd_date = datetime.strptime(gd["gameDate"], "%m/%d/%Y %H:%M:%S").strftime("%Y%m%d")
        if gd_date != target:
            continue
        for g in gd.get("games", []):
            away = f"{g['awayTeam']['teamCity']} {g['awayTeam']['teamName']}".strip()
            home = f"{g['homeTeam']['teamCity']} {g['homeTeam']['teamName']}".strip()
            start = format_time(g.get("gameDateTimeUTC"))
            games.append(f"{away} @ {home} — {start}")
    return games


def fetch_world_cup(date: str) -> List[str]:
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
        f"?dates={date.replace('-', '')}"
    )
    data = requests.get(url, timeout=TIMEOUT).json()
    games = []
    for event in data.get("events", []):
        comps = event.get("competitions", [{}])[0].get("competitors", [])
        if len(comps) < 2:
            continue
        home = next((c["team"]["displayName"] for c in comps if c.get("homeAway") == "home"), "")
        away = next((c["team"]["displayName"] for c in comps if c.get("homeAway") == "away"), "")
        start = format_time(event.get("date"))
        games.append(f"{away} vs {home} — {start}")
    return games


def format_time(iso: str | None) -> str:
    if not iso:
        return "TBD"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Toronto"))
    return dt.strftime("%-I:%M %p ET")


def section(title: str, games: List[str]) -> str:
    body = "\n".join(f"  • {g}" for g in games) if games else f"  {NO_GAME}"
    return f"{title}\n{body}"


def safe_fetch(fn, date: str) -> List[str]:
    try:
        return fn(date)
    except Exception as e:
        return [f"(error fetching: {e})"]


def build_email_body(date: str) -> str:
    return "\n\n".join([
        f"Today's Games — {date}",
        section("MLB", safe_fetch(fetch_mlb, date)),
        section("NBA", safe_fetch(fetch_nba, date)),
        section("World Cup", safe_fetch(fetch_world_cup, date)),
    ])


def send_email(body: str, date: str) -> None:
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
    msg.set_content(body)

    with smtplib.SMTP(host, port) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, password)
        s.send_message(msg)


def main() -> None:
    tz = ZoneInfo("America/Toronto")
    now = datetime.now(tz)
    date = now.strftime("%Y-%m-%d")
    body = build_email_body(date)
    if os.environ.get("DRY_RUN") == "1":
        print(body)
        return
    if os.environ.get("ENFORCE_9AM") == "1" and now.hour != 9:
        print(f"Skipping: local hour is {now.hour}, not 9.")
        return
    send_email(body, date)


if __name__ == "__main__":
    main()
