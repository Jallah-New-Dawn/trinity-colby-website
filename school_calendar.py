"""Yearly refresh of the USD 315 Colby school-year dates used by the Confirmation pill entry.

Run once a year (GitHub Actions does this every July 1, see .github/workflows/school-dates.yml).
It downloads the district's BOE-approved calendar PDF, reads the first and last day of school for
students from the calendar grid, and rewrites school_dates.json. generate.py / js_base.py read that
file at build time, and the live pages read it at runtime from the repository.

If the download or any check fails, the existing school_dates.json is left untouched and the script
exits with an error, so a bad fetch can never blank out the schedule.

How the PDF is read: the calendar has two columns of month grids (July-December on the left,
January-June on the right). Each event row sits in the same vertical band as its month's grid, so an
event's month is the grid block in its band and column. Each grid is checked against the real
calendar for its month, so a layout change is caught rather than misread.
"""
import io
import json
import os
import re
import sys
import urllib.request
from calendar import monthrange
from datetime import date

from pypdf import PdfReader

HERE = os.path.dirname(os.path.abspath(__file__))
DATES_PATH = os.path.join(HERE, "school_dates.json")

# Calendar sources, tried in order. The district posts each year's calendar at a new address,
# so update this link once a year (the district's document system, colbyeagles.org).
SOURCES = [
    "https://files-backend.assets.thrillshare.com/documents/asset/uploaded_file/2585/Colby/50d09b12-728d-4342-93e0-2def643a1595/2026-27-Academic-Calendar---Approved-KB-(1).pdf?disposition=inline",
]

# Event labels that mark the first and last day of school for students.
FIRST_DAY_LABEL = "Begin 1st quarter"  # "Begin 1st quarter - First student day"
LAST_DAY_LABEL = "End 4th Qtr"          # "End 4th Qtr/2nd semester"

LEFT_MONTHS = [7, 8, 9, 10, 11, 12]     # top to bottom, left column
RIGHT_MONTHS = [1, 2, 3, 4, 5, 6]       # top to bottom, right column
GRID_LEFT_MAX_X = 200                   # left-column grids sit left of this x
RIGHT_GRID_MIN_X = 600                  # right-column grids start at or right of this x
RIGHT_EVENTS_MIN_X = 300                # event labels at or right of this x belong to the right column


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def page_items(data):
    """(x, y, text) for every text run on the first page."""
    items = []

    def visitor(text, cm, tm, fd, fs):
        t = text.strip()
        if t:
            items.append((tm[4], tm[5], t))

    PdfReader(io.BytesIO(data)).pages[0].extract_text(visitor_text=visitor)
    return items


def cluster(vals, tol):
    out = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def grid_blocks(items):
    """Group the calendar's day numbers into month grids: {"left": [...], "right": [...]}, top first."""
    digits = [(y, x, int(t)) for x, y, t in items if t.isdigit() and 1 <= int(t) <= 31]
    # a grid column repeats its x positions on every row; event day numbers do not
    counts = {}
    for y, x, d in digits:
        counts[round(x)] = counts.get(round(x), 0) + 1
    xs = sorted(x for x, n in counts.items() if n >= 6)
    groups, cur = [], []
    for x in xs:
        if cur and x - cur[-1] > 25:  # grid columns are ~18 pt apart; the event column sits ~35 pt past them
            groups.append(cur)
            cur = []
        cur.append(x)
    if cur:
        groups.append(cur)

    out = {"left": [], "right": []}
    for g in groups:
        if min(g) < GRID_LEFT_MAX_X:
            column = "left"
        elif min(g) >= RIGHT_GRID_MIN_X:
            column = "right"
        else:
            continue
        lo, hi = min(g) - 5, max(g) + 5
        toks = sorted(((y, x, d) for y, x, d in digits if lo <= x <= hi), reverse=True)
        # split the column into month grids wherever there is a vertical gap
        block, last_y = [], None
        for y, x, d in toks:
            if last_y is not None and last_y - y > 30:
                if len(block) >= 20:  # a full month has at least 28 day numbers
                    out[column].append(block)
                block = []
            block.append((y, x, d))
            last_y = y
        if len(block) >= 20:
            out[column].append(block)
    for column in out:
        out[column].sort(key=lambda b: -max(y for y, x, d in b))
    return out


def month_matches(block, year, month):
    """True if this grid's printed days sit in the right cells for year-month."""
    first = date(year, month, 1).weekday()      # Monday = 0
    w0 = (first + 1) % 7                        # Sunday = 0, the offset of the 1st in the grid
    n = monthrange(year, month)[1]
    cols = cluster([x for y, x, d in block], 3)[:7]
    rows = cluster([y for y, x, d in block], 6)

    def near(vals, v):
        return min(range(len(vals)), key=lambda i: abs(vals[i] - v))

    cells = set()
    for y, x, d in block:
        if min(abs(c - x) for c in cols) > 3:
            continue
        r, c = len(rows) - 1 - near(rows, y), near(cols, x)  # row 0 is the top row
        index = w0 + d - 1 if d <= 28 else None
        cell = r * 7 + c
        if cell < w0 or cell >= w0 + n:
            continue  # a neighbouring month's day shown in grey
        cells.add((r, c, d))
    expected = {((w0 + d - 1) // 7, (w0 + d - 1) % 7, d) for d in range(1, n + 1)}
    # every printed day of this month sits in its cell; a few days may be missing from the printed grid
    return cells <= expected and len(cells) >= len(expected) - 3


def event_date(items, grids, label, first_year):
    """The ISO date of the single event whose label contains `label`."""
    hits = []
    for lx, ly, lt in items:
        if label not in lt:
            continue
        column = "right" if lx >= RIGHT_EVENTS_MIN_X else "left"
        months = RIGHT_MONTHS if column == "right" else LEFT_MONTHS
        bands = [b for b in grids[column]
                 if min(y for y, x, d in b) - 8 <= ly <= max(y for y, x, d in b) + 8]
        if len(bands) != 1:
            continue
        index = grids[column].index(bands[0])
        if index >= len(months):
            continue
        month = months[index]
        year = first_year + 1 if column == "right" else first_year
        if not month_matches(bands[0], year, month):
            raise ValueError(f"the {column} grid #{index + 1} does not match {year}-{month:02d}")
        # the event's day number sits just left of its label, on the same row
        days = [int(t) for x2, y2, t in items
                if t.isdigit() and abs(y2 - ly) <= 4 and lx - 25 <= x2 < lx]
        if days:
            hits.append(date(year, month, days[0]).isoformat())
    if len(set(hits)) != 1:
        raise ValueError(f"expected one date for '{label}', found {sorted(set(hits)) or 'none'}")
    return hits[0]


def find_school_year(data):
    items = page_items(data)
    grids = grid_blocks(items)
    if len(grids["left"]) != 6 or len(grids["right"]) != 6:
        raise ValueError(f"expected 6 month grids per column, found {len(grids['left'])} and {len(grids['right'])}")
    first_year = None
    for x, y, t in items:
        m = re.fullmatch(r"(\d{4}) - (\d{4})", t)
        if m:
            first_year = int(m.group(1))
    if first_year is None:
        raise ValueError("school year header (e.g. '2026 - 2027') not found")
    first = event_date(items, grids, FIRST_DAY_LABEL, first_year)
    last = event_date(items, grids, LAST_DAY_LABEL, first_year)
    if not first < last:
        raise ValueError(f"first day {first} is not before last day {last}")
    return first, last


def main():
    errors = []
    for url in SOURCES:
        try:
            first, last = find_school_year(fetch(url))
        except Exception as e:  # try the next source
            errors.append(f"{url}: {e}")
            continue
        start = int(first[:4])
        data = {
            "school_year": f"{start}-{str(start + 1)[2:]}",
            "first_day": first,
            "last_day": last,
            # Confirmation runs for the whole school year, first day through last day for students.
            "confirmation_from": first,
            "confirmation_to": last,
            "source": url,
            "verified": date.today().isoformat(),
        }
        with open(DATES_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        print(f"school_dates.json updated for {data['school_year']}: {first} to {last}")
        return 0
    print("NOT updated. Every source failed:", *errors, sep="\n  ", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
