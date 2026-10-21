#!/usr/bin/env python3
"""Download La Joya ISD CNS menu PDFs and convert their calendar grids to CSV.

Output CSV columns: date, weekday, breakfast, lunch

Usage:
    python lajoya_menus.py [--out-dir DIR] [--url PAGE_URL] [-v]
    python lajoya_menus.py --parse-only some.pdf [other.pdf ...]   # no network
"""
import argparse
import calendar
import csv
import datetime as dt
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup

PAGE_URL = "https://lajoyaisdcns.com/index.php?page=menus&sid=0208131445498761"
USER_AGENT = "Mozilla/5.0 (menu-to-csv script)"

log = logging.getLogger("lajoya_menus")

WEEKDAYS = {
    "monday": 0, "mon": 0, "lunes": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "martes": 1,
    "wednesday": 2, "wed": 2, "miercoles": 2, "miércoles": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "jueves": 3,
    "friday": 4, "fri": 4, "viernes": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS.update({"sept": 9, "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
               "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10,
               "noviembre": 11, "diciembre": 12})
MEAL_SPLIT_RE = re.compile(r"(?i)\b(breakfast|desayuno|lunch|almuerzo)\b\s*:?")
DAY_RE = re.compile(r"^\s*(\d{1,2})(?:st|nd|rd|th)?\b[.:)\-]?\s*(.*)$", re.S)


# --------------------------------------------------------------------------- fetch

def find_menu_links(session, page_url):
    """Return [(absolute_url, link_text)] for every downloadMenu.php link on the page."""
    resp = session.get(page_url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        if "downloadmenu.php" not in a["href"].lower():
            continue
        url = urljoin(page_url, a["href"])
        if url in seen:
            continue
        seen.add(url)
        text = " ".join(a.get_text(" ", strip=True).split()) or a.get("title", "")
        links.append((url, text))
    return links


def download_pdf(session, url, dest_dir):
    """Download url (following redirects) into dest_dir; return the saved path."""
    resp = session.get(url, timeout=60, allow_redirects=True)
    resp.raise_for_status()
    if not resp.content.startswith(b"%PDF"):
        raise ValueError(f"{url} did not return a PDF (content-type {resp.headers.get('content-type')})")
    menu_id = url.rstrip("/").rsplit("/", 1)[-1]
    name = Path(resp.url.split("?")[0]).name
    if not name.lower().endswith(".pdf"):
        name = "menu.pdf"
    path = dest_dir / f"{menu_id}_{re.sub(r'[^A-Za-z0-9._-]+', '_', name)}"
    path.write_bytes(resp.content)
    return path


# --------------------------------------------------------------------------- parse

def guess_meal(*texts):
    """'breakfast', 'lunch', or None (combined / unknown) from labels and PDF text."""
    for text in texts:
        t = (text or "").lower()
        b = "breakfast" in t or "desayuno" in t
        l = "lunch" in t or "almuerzo" in t
        if b != l:
            return "breakfast" if b else "lunch"
    return None


def guess_month_year(*texts, today=None):
    """Find 'October 2026' style month/year. Falls back to the nearest plausible year."""
    today = today or dt.date.today()
    month = year = None
    for text in texts:
        if not text:
            continue
        for m in re.finditer(r"(?i)\b([a-zé]{3,10})\.?,?\s*(?:de\s+)?(20\d\d)?\b", text):
            mo = MONTHS.get(m.group(1).lower())
            if mo:
                month = month or mo
                if m.group(2) and mo == month:
                    year = int(m.group(2))
                    break
        if month and year:
            break
    if month and not year:
        # School-year menus: pick the year that puts the month closest to today.
        year = min((today.year - 1, today.year, today.year + 1),
                   key=lambda y: abs((dt.date(y, month, 15) - today).days))
    return month, year


def clean_text(text):
    lines = [" ".join(l.split()) for l in (text or "").splitlines()]
    return "; ".join(l for l in lines if l)


def split_meals(text):
    """Split combined cell text into {'breakfast': ..., 'lunch': ...} using labels."""
    parts = MEAL_SPLIT_RE.split(text)
    if len(parts) == 1:
        return {}
    out = {}
    for label, body in zip(parts[1::2], parts[2::2]):
        key = "breakfast" if label.lower() in ("breakfast", "desayuno") else "lunch"
        out[key] = clean_text(body)
    return out


def _valid_day(day, weekday, month, year):
    try:
        d = dt.date(year, month, day)
    except ValueError:
        return None
    return d if weekday is None or d.weekday() == weekday else None


def _header_weekdays(row):
    """Map column index -> weekday number if this row looks like a weekday header."""
    cols = {}
    for i, cell in enumerate(row):
        word = re.sub(r"[^a-záéíóú]", "", (cell or "").lower())
        if word in WEEKDAYS:
            cols[i] = WEEKDAYS[word]
    return cols if len(cols) >= 3 else None


def parse_tables(page, month, year):
    """Strategy 1: ruled table grid. Returns {date: cell_text}."""
    cells = {}
    for table in page.extract_tables():
        header = None
        for row in table:
            hdr = _header_weekdays(row)
            if hdr:
                header = hdr
                continue
            if not header:
                continue
            for i, cell in enumerate(row):
                m = DAY_RE.match(cell or "")
                if i not in header or not m:
                    continue
                d = _valid_day(int(m.group(1)), header[i], month, year)
                if d:
                    cells[d] = m.group(2)
    return cells


def parse_words(page, month, year):
    """Strategy 2: position-based grid (no ruling lines). Returns {date: cell_text}."""
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    headers = []
    for w in words:
        key = re.sub(r"[^a-záéíóú]", "", w["text"].lower())
        if key in WEEKDAYS and len(key) >= 3:
            headers.append(((w["x0"] + w["x1"]) / 2, w["bottom"], WEEKDAYS[key]))
    if len(headers) < 3:
        return {}
    # Keep the header row (most common y position) only.
    ys = [round(h[1]) for h in headers]
    hdr_y = max(set(ys), key=ys.count)
    headers = sorted({h[2]: h for h in headers if abs(h[1] - hdr_y) < 5}.values())
    centers = [h[0] for h in headers]
    bounds = [-1e9] + [(a + b) / 2 for a, b in zip(centers, centers[1:])] + [1e9]

    def col_of(x):
        for i in range(len(centers)):
            if bounds[i] <= x < bounds[i + 1]:
                return i
        return None

    # Day numbers: integer tokens whose value matches the column's weekday this month.
    day_marks = []  # (top, col, date)
    for w in words:
        if w["top"] <= hdr_y or not re.fullmatch(r"\d{1,2}", w["text"]):
            continue
        c = col_of((w["x0"] + w["x1"]) / 2)
        if c is None:
            continue
        d = _valid_day(int(w["text"]), headers[c][2], month, year)
        if d:
            day_marks.append((w["top"], c, d, w))
    # A menu item like "1 Milk" can also match; the real day label is the topmost one.
    first = {}
    for m in sorted(day_marks, key=lambda m: m[0]):
        first.setdefault(m[2], m)
    day_marks = list(first.values())
    if not day_marks:
        return {}
    # Row tops = clusters of day-number tops.
    row_tops = []
    for top in sorted(m[0] for m in day_marks):
        if not row_tops or top - row_tops[-1] > 8:
            row_tops.append(top)
    row_tops.append(page.height)

    cells = {}
    for top, c, d, mark in day_marks:
        r = max(i for i, t in enumerate(row_tops[:-1]) if t <= top + 8)
        y0, y1 = row_tops[r] - 2, row_tops[r + 1] - 2
        x0, x1 = bounds[c], bounds[c + 1]
        in_cell = [w for w in words if w is not mark and y0 <= w["top"] < y1
                   and x0 <= (w["x0"] + w["x1"]) / 2 < x1]
        lines = {}
        for w in sorted(in_cell, key=lambda w: (round(w["top"]), w["x0"])):
            lines.setdefault(round(w["top"] / 3), []).append(w["text"])
        cells[d] = "\n".join(" ".join(v) for _, v in sorted(lines.items()))
    return cells


def parse_pdf(path, label="", today=None):
    """Return (meal, {date: {'breakfast': str, 'lunch': str}}) for one menu PDF."""
    with pdfplumber.open(path) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        month, year = guess_month_year(label, full_text, today=today)
        if not month:
            log.warning("%s: could not determine month; skipping", path)
            return None, {}
        meal = guess_meal(label, full_text[:500])
        cells = {}
        for page in pdf.pages:
            found = parse_tables(page, month, year)
            if len(found) < 5:  # table extraction missed the grid; try word positions
                alt = parse_words(page, month, year)
                if len(alt) > len(found):
                    found = alt
            cells.update(found)
    result = {}
    for d, text in cells.items():
        split = split_meals(text)
        if split:
            result[d] = split
        elif meal:
            result[d] = {meal: clean_text(text)}
        else:
            result[d] = {"lunch": clean_text(text)}  # unlabeled: assume lunch
    log.info("%s: %s %d-%02d, %d days", Path(path).name, meal or "combined", year, month, len(result))
    return meal, result


# --------------------------------------------------------------------------- csv

FIELDS = ["date", "weekday", "breakfast", "lunch"]


def load_csv(path):
    rows = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows[dt.date.fromisoformat(row["date"])] = {
                    "breakfast": row.get("breakfast", ""), "lunch": row.get("lunch", "")}
    return rows


def write_csv(path, rows):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for d in sorted(rows):
            w.writerow({"date": d.isoformat(), "weekday": d.strftime("%A"),
                        "breakfast": rows[d].get("breakfast", ""),
                        "lunch": rows[d].get("lunch", "")})
    tmp.replace(path)


def merge(rows, parsed):
    for d, meals in parsed.items():
        slot = rows.setdefault(d, {"breakfast": "", "lunch": ""})
        for k, v in meals.items():
            if v:
                slot[k] = v


# --------------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=PAGE_URL)
    ap.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent / "output")
    ap.add_argument("--parse-only", nargs="+", type=Path, metavar="PDF",
                    help="parse local PDFs instead of downloading")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    pdf_dir = args.out_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "menus.csv"
    rows = load_csv(csv_path)  # keep earlier months once they drop off the site

    if args.parse_only:
        sources = [(p, p.stem) for p in args.parse_only]
    else:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        links = find_menu_links(session, args.url)
        log.info("found %d menu links", len(links))
        if not links:
            log.error("no downloadMenu.php links found on %s", args.url)
            return 1
        sources = []
        for url, text in links:
            try:
                sources.append((download_pdf(session, url, pdf_dir), text))
            except Exception as e:  # one bad link shouldn't kill the run
                log.warning("download failed for %s: %s", url, e)

    parsed_any = False
    for path, label in sources:
        try:
            _, parsed = parse_pdf(path, label)
        except Exception as e:
            log.warning("parse failed for %s: %s", path, e)
            continue
        parsed_any |= bool(parsed)
        merge(rows, parsed)

    write_csv(csv_path, rows)
    log.info("wrote %d rows to %s", len(rows), csv_path)
    return 0 if parsed_any else 2


if __name__ == "__main__":
    sys.exit(main())
