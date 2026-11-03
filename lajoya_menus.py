#!/usr/bin/env python3
"""Download La Joya ISD High School menu PDFs and save them as output/menus.csv.

In each day's box, breakfast is printed in red and lunch in black.
"""
import csv
import datetime as dt
import re
from pathlib import Path

import pdfplumber
import requests

SITE = "https://lajoyaisdcns.com"
MENUS_PAGE = SITE + "/index.php?sid=0208131445498761&page=menus&sm={month}&sy={year}"
WHICH_MENU = "High School"
OUT = Path(__file__).resolve().parent / "output"
CSV_PATH = OUT / "menus.csv"


def menu_links(month, year):
    html = requests.get(MENUS_PAGE.format(month=month, year=year), timeout=30).text
    links = re.findall(r'href="(/downloadMenu\.php/[^"]+)"[^>]*>([^<]+)', html)
    return [SITE + href for href, title in links if WHICH_MENU.lower() in title.lower()]


def is_red(word):
    color = word.get("non_stroking_color") or ()
    return len(color) == 3 and color[0] > 0.6 and color[1] < 0.4 and color[2] < 0.4


def clusters(values, gap=10):
    """Group nearly equal positions (the left edges of columns, the tops of rows)."""
    groups = []
    for v in sorted(values):
        if not groups or v - groups[-1] > gap:
            groups.append(v)
    return groups


def join_lines(lines):
    """Rejoin dishes that wrapped onto two lines, e.g. 'Beef Steak Fingers' + 'w/ Biscuit'."""
    items = []
    for line in lines:
        if items and (line.startswith(("w/", "&")) or items[-1].endswith(("w/", "&"))):
            items[-1] += " " + line
        else:
            items.append(line)
    return "; ".join(items)


def parse_pdf(pdf_bytes, path):
    path.write_bytes(pdf_bytes)
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        month_name, year = re.search(r"([A-Z][a-z]+) (20\d\d)", page.extract_text()).groups()
        month = dt.datetime.strptime(month_name, "%B").month
        words = page.extract_words(extra_attrs=["non_stroking_color"])

    # Each day's box starts with a two-digit day number in its top-left corner.
    days = [w for w in words if re.fullmatch(r"\d\d", w["text"]) and int(w["text"]) <= 31]
    col_starts = clusters(w["x0"] for w in days)
    row_tops = clusters(w["top"] for w in days)
    row_tops.append(row_tops[-1] + (row_tops[-1] - row_tops[-2]))  # bottom of the last row
    col_starts.append(col_starts[-1] + (col_starts[-1] - col_starts[-2]))

    menus = {}
    for day in days:
        c = max(i for i, x in enumerate(col_starts) if x <= day["x0"] + 5)
        r = max(i for i, y in enumerate(row_tops) if y <= day["top"] + 5)
        x0, x1 = col_starts[c] - 2, col_starts[c + 1] - 2
        y0, y1 = row_tops[r] - 14, row_tops[r + 1] - 14
        lines = {}
        for w in words:
            if w is not day and x0 <= w["x0"] < x1 and y0 <= w["top"] < y1:
                lines.setdefault(round(w["top"] / 4), []).append(w)
        breakfast, lunch = [], []
        for _, line in sorted(lines.items()):
            line.sort(key=lambda w: w["x0"])
            text = " ".join(w["text"] for w in line)
            text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)  # "ButterymapleWaffle" -> "Butterymaple Waffle"
            # Red lines at the top of the box are breakfast; everything after is lunch.
            if is_red(line[0]) and not lunch:
                breakfast.append(text)
            else:
                lunch.append(text)
        date = dt.date(int(year), month, int(day["text"]))
        menus[date] = {"breakfast": join_lines(breakfast), "lunch": join_lines(lunch)}
    return menus


def main():
    OUT.mkdir(exist_ok=True)
    rows = {}
    if CSV_PATH.exists():  # keep earlier months after they leave the website
        with CSV_PATH.open(newline="", encoding="utf-8") as f:
            rows = {dt.date.fromisoformat(r["date"]): r for r in csv.DictReader(f)}

    first = dt.date.today().replace(day=1)
    last_month, next_month = first - dt.timedelta(days=1), first + dt.timedelta(days=32)
    for d in (last_month, first, next_month):
        for url in menu_links(d.month, d.year):
            pdf = requests.get(url, timeout=60).content
            for date, meals in parse_pdf(pdf, OUT / "latest.pdf").items():
                rows[date] = {"date": date.isoformat(), "weekday": date.strftime("%A"), **meals}
            print(f"read {url}")

    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "weekday", "breakfast", "lunch"])
        writer.writeheader()
        writer.writerows(rows[d] for d in sorted(rows))
    print(f"{len(rows)} days in {CSV_PATH}")
    write_weeks(rows)


def write_weeks(rows):
    """One file per school week, laid out like the Canva table: weekdays, breakfast row, lunch row."""
    weeks_dir = OUT / "weeks"
    weeks_dir.mkdir(exist_ok=True)
    mondays = sorted({d - dt.timedelta(days=d.weekday()) for d in rows})
    for monday in mondays:
        days = [monday + dt.timedelta(days=i) for i in range(5)]
        cell = lambda d, meal: rows[d][meal].replace("; ", "\n") if d in rows else ""
        with (weeks_dir / f"{monday.isoformat()}.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([d.strftime("%A") for d in days])
            writer.writerow([cell(d, "breakfast") for d in days])
            writer.writerow([cell(d, "lunch") for d in days])
    print(f"{len(mondays)} weekly tables in {weeks_dir}")


if __name__ == "__main__":
    main()
