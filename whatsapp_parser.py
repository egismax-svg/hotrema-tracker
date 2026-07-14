"""
SCAFF TRACK - WhatsApp Chat Parser
==================================
Reads an exported WhatsApp chat file ('_chat.txt') and extracts scaffold
records into a clean CSV spreadsheet.

Usage:
    python whatsapp_parser.py                     # reads _chat.txt -> scaffolds.csv
    python whatsapp_parser.py mychat.txt out.csv  # custom paths

Rules implemented:
  1. Regex field extraction: Job Number, Client (C-), Location, Height,
     Width ("Wide"), Length, Request Number (R-), Supervisor, Type (J-).
  2. Automatic volume: H x W x L, handles '4,25' and '4.25', rounded to 2 dp.
  3. Hotrema rule: internal Hotrema builds never carry a Request Number --
     the R- field is forced to blank.
  4. Smart dismantle logic:
       - 'Dismantled' / 'Dismantle' / 'Demontage' keywords mark the
         referenced Job # or Request # as 'Dismantled'.
       - No chat activity for > 30 days (relative to the newest message
         in the export) -> 'Dismantled by Expiry'.
       - Everything else -> 'Active'.
"""

import csv
import re
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Message header formats (iOS and Android exports)
# ---------------------------------------------------------------------------
IOS_HEADER = re.compile(
    r"^\[(\d{1,2}[./]\d{1,2}[./]\d{2,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?)"
    r"(?:\s*[APap]\.?[Mm]\.?)?\]\s*([^:]+?):\s?(.*)$"
)
ANDROID_HEADER = re.compile(
    r"^(\d{1,2}[./]\d{1,2}[./]\d{2,4}),?\s+(\d{1,2}:\d{2})"
    r"(?:\s*[APap]\.?[Mm]\.?)?\s*-\s*([^:]+?):\s?(.*)$"
)

DATE_FORMATS = ("%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y", "%d.%m.%y", "%m/%d/%y", "%m/%d/%Y")

# ---------------------------------------------------------------------------
# Field extraction patterns
# ---------------------------------------------------------------------------
FIELD_PATTERNS = {
    "job":        re.compile(r"\bjob\s*(?:number|no\.?|nr\.?|#)?\s*[:\-]?\s*#?\s*([A-Za-z]?\d[\w/\-]*)", re.I),
    "client":     re.compile(r"\bC\s?-\s*([^\n,;|]+)"),
    "request":    re.compile(r"\bR\s?-\s*#?\s*([A-Za-z0-9/\-]+)"),
    "type":       re.compile(r"\bJ\s?-\s*([^\n,;|]+)"),
    "location":   re.compile(r"\blocation\s*[:\-]?\s*([^\n]+)", re.I),
    "altitude":   re.compile(r"\baltitude\s*[:\-]?\s*(\d+(?:[.,]\d+)?)", re.I),
    "height":     re.compile(r"\bheights?\s*[:\-]?\s*(\d+(?:[.,]\d+)?)", re.I),
    "width":      re.compile(r"\b(?:wide|width)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)", re.I),
    "length":     re.compile(r"\blength\s*[:\-]?\s*(\d+(?:[.,]\d+)?)", re.I),
    "supervisor": re.compile(r"\bsupervisor\s*[:\-]?\s*([^\n]+)", re.I),
}

DISMANTLE_RE = re.compile(r"\b(dismantl\w*|demontage)\b", re.I)
EXPIRY_DAYS = 30


def parse_date(raw: str) -> datetime | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def to_float(raw: str) -> float | None:
    """Convert '4,25' or '4.25' (or '1 250,5') to float."""
    if not raw:
        return None
    cleaned = raw.strip().replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def read_messages(path: Path) -> list[dict]:
    """Parse the raw export into [{date, sender, text}], merging
    continuation lines into their parent message."""
    messages = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            # WhatsApp sprinkles invisible direction marks into exports
            line = line.replace("‎", "").replace("‏", "").rstrip("\n")
            m = IOS_HEADER.match(line) or ANDROID_HEADER.match(line)
            if m:
                date = parse_date(m.group(1))
                messages.append({"date": date, "sender": m.group(3).strip(), "text": m.group(4)})
            elif messages and line.strip():
                # continuation line of a multi-line message
                messages[-1]["text"] += "\n" + line
    return [m for m in messages if m["date"] is not None]


def extract_fields(text: str) -> dict:
    out = {}
    for key, pattern in FIELD_PATTERNS.items():
        m = pattern.search(text)
        if m:
            out[key] = m.group(1).strip().rstrip(".")
    return out


def is_hotrema(client: str | None) -> bool:
    return bool(client) and "hotrema" in client.lower()


def build_records(messages: list[dict]) -> list[dict]:
    records: dict[str, dict] = {}   # keyed by job number (or request as fallback)

    def find_record(fields: dict) -> dict | None:
        if fields.get("job") and fields["job"] in records:
            return records[fields["job"]]
        if fields.get("request"):
            for rec in records.values():
                if rec["request"] and rec["request"].lower() == fields["request"].lower():
                    return rec
        return None

    for msg in messages:
        fields = extract_fields(msg["text"])
        if not fields:
            continue

        dismantle_hit = DISMANTLE_RE.search(msg["text"]) is not None

        rec = find_record(fields)
        if rec is None:
            key = fields.get("job") or (f"R-{fields['request']}" if fields.get("request") else None)
            if key is None:
                continue  # nothing identifiable to anchor a record to
            rec = {
                "job": fields.get("job", ""),
                "client": "", "location": "", "request": "",
                "supervisor": "", "type": "",
                "height": None, "width": None, "length": None, "altitude": None,
                "first_seen": msg["date"], "last_activity": msg["date"],
                "messages": 0, "status": "Active",
            }
            records[key] = rec

        # merge newly-seen values (later messages can fill gaps / update)
        for k in ("client", "location", "supervisor", "type", "request"):
            if fields.get(k):
                rec[k] = fields[k]
        for k in ("height", "width", "length", "altitude"):
            if fields.get(k):
                val = to_float(fields[k])
                if val is not None:
                    rec[k] = val

        rec["messages"] += 1
        rec["last_activity"] = max(rec["last_activity"], msg["date"])
        if rec["first_seen"] > msg["date"]:
            rec["first_seen"] = msg["date"]

        if dismantle_hit:
            rec["status"] = "Dismantled"

    # --- Hotrema rule: internal builds carry no request number ---
    # --- altitude may be written inline as "Location - X (24m)" ---
    alt_in_loc = re.compile(r"^(.*?)\s*\((\d+(?:[.,]\d+)?)\s*m\)$", re.I)
    for rec in records.values():
        if is_hotrema(rec["client"]):
            rec["request"] = ""
        m = alt_in_loc.match(rec["location"])
        if m:
            rec["location"] = m.group(1).strip()
            if rec["altitude"] is None:
                rec["altitude"] = to_float(m.group(2))

    # --- 30-day expiry rule (relative to the newest message in the chat) ---
    if messages:
        reference = max(m["date"] for m in messages)
        for rec in records.values():
            if rec["status"] == "Active" and (reference - rec["last_activity"]).days > EXPIRY_DAYS:
                rec["status"] = "Dismantled by Expiry"

    return list(records.values())


def volume(rec: dict) -> str:
    dims = (rec["height"], rec["width"], rec["length"])
    if all(d is not None for d in dims):
        return f"{round(dims[0] * dims[1] * dims[2], 2):.2f}"
    return ""


def write_csv(records: list[dict], out_path: Path) -> None:
    header = [
        "Job Number", "Client", "Location", "Altitude (m)", "Height (m)", "Width (m)",
        "Length (m)", "Volume (m3)", "Request Number", "Supervisor",
        "Scaffold Type", "Status", "First Seen", "Last Activity", "Messages",
    ]
    # utf-8-sig so Excel opens it with correct characters
    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for rec in sorted(records, key=lambda r: (r["status"], r["job"])):
            writer.writerow([
                rec["job"], rec["client"], rec["location"],
                "" if rec["altitude"] is None else rec["altitude"],
                "" if rec["height"] is None else rec["height"],
                "" if rec["width"] is None else rec["width"],
                "" if rec["length"] is None else rec["length"],
                volume(rec),
                rec["request"], rec["supervisor"], rec["type"], rec["status"],
                rec["first_seen"].strftime("%Y-%m-%d"),
                rec["last_activity"].strftime("%Y-%m-%d"),
                rec["messages"],
            ])


def main() -> None:
    chat_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_chat.txt")
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("scaffolds.csv")

    if not chat_path.exists():
        sys.exit(f"Chat file not found: {chat_path}")

    messages = read_messages(chat_path)
    print(f"Parsed {len(messages)} messages from {chat_path}")

    records = build_records(messages)
    write_csv(records, out_path)

    active = sum(1 for r in records if r["status"] == "Active")
    dism = sum(1 for r in records if r["status"] == "Dismantled")
    exp = sum(1 for r in records if r["status"] == "Dismantled by Expiry")
    print(f"Extracted {len(records)} scaffolds -> {out_path}")
    print(f"  Active: {active} | Dismantled: {dism} | Dismantled by Expiry: {exp}")


if __name__ == "__main__":
    main()
