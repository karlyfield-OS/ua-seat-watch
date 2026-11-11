"""Check publicly listed UA Banner sections once and alert on new availability."""

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BANNER_URL = "https://bannerssb.ua.edu/StudentRegistrationSsb/ssb/term/termSelection?mode=search"
TERM = "Spring 2027"
LAST_CHECK_DATE = date(2027, 1, 20)
CENTRAL = ZoneInfo("America/Chicago")
COURSES = ("HSM 338", "HSM 404", "HSM 425", "HSM 436", "MGT 386", "MGT 387", "MGT 406")
CAMPUSES = ("Main Campus (Tuscaloosa)", "Online - Main Campus Students")
FIELDS = ("subject", "courseNumber", "sequenceNumber", "courseReferenceNumber",
          "courseTitle", "meetingTime", "campus", "status")
RESULT_HEADING = re.compile(r"Search Results(?:\s*[—–-]\s*|\s+)([\d,]+)\s+Class(?:es)?\b", re.I)
SEAT_STATUS = re.compile(r"\b(\d+)\s+of\s+(\d+)\s+seats?\s+remain\b", re.I)
TIME_RANGE = re.compile(r"(\d{1,2}:\d{2}\s*[AP]M)\s*[-–—]\s*(\d{1,2}:\d{2}\s*[AP]M)", re.I)
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAY_CODES = "MTWRFSU"
STATE_VERSION = 1


class WatchError(RuntimeError):
    """An incomplete check that must not overwrite the previous snapshot."""


@dataclass(frozen=True)
class Meeting:
    days: frozenset[int]
    start: int
    end: int

    def overlaps(self, other: "Meeting") -> bool:
        return bool(self.days & other.days) and self.start < other.end and other.start < self.end

    def label(self) -> str:
        days = "".join(DAY_CODES[day] for day in sorted(self.days))
        return f"{days} {clock_label(self.start)}–{clock_label(self.end)}"


@dataclass(frozen=True)
class Schedule:
    hard: tuple[Meeting, ...] = ()
    soft: tuple[Meeting, ...] = ()


@dataclass(frozen=True)
class Section:
    course: str
    sequence: str
    crn: str
    title: str
    campus: str
    seats: int
    capacity: int | None
    meetings: tuple[Meeting, ...]
    time_unverified: bool = False

    def eligibility(self, schedule: Schedule) -> str:
        if not self.seats:
            return "closed"
        if any(meeting.overlaps(blocked) for meeting in self.meetings for blocked in schedule.hard):
            return "conflict"
        if any(meeting.overlaps(blocked) for meeting in self.meetings for blocked in schedule.soft):
            return "swap"
        return "open"

    def label(self, schedule: Schedule) -> str:
        timing = ", ".join(meeting.label() for meeting in self.meetings)
        if self.time_unverified:
            timing = f"{timing}, verify unlisted time" if timing else "verify unlisted time"
        elif not timing:
            timing = "online, no scheduled meeting shown"
        swap = " · overlaps a replaceable class" if self.eligibility(schedule) == "swap" else ""
        campus = "online, main-campus students" if self.campus == CAMPUSES[1] else "Tuscaloosa"
        seats = f"{self.seats}/{self.capacity}" if self.capacity is not None else str(self.seats)
        return f"{self.course}-{self.sequence} · CRN {self.crn} · {seats} seats · {campus} · {timing}{swap}"


def normalize(text: str) -> str:
    return " ".join(text.split())


def read_seats(text: str) -> tuple[int, int | None]:
    """Read ordinary seats only. Banner reports waitlist capacity in the same cell."""
    text = normalize(text)
    if re.search(r"\bcancell?ed\b", text, re.I):
        return 0, None
    matches = SEAT_STATUS.findall(text)
    if len(matches) != 1:
        raise WatchError(f"Could not identify one ordinary seat count in {text!r}.")
    seats, capacity = (int(value) for value in matches[0])
    if seats > capacity or (seats > 0 and re.search(r"\bFULL\s*:", text, re.I)):
        raise WatchError(f"Banner showed contradictory seat counts in {text!r}.")
    return seats, capacity


def clock_minutes(value: str) -> int:
    parsed = datetime.strptime(value.replace(" ", "").upper(), "%I:%M%p")
    return parsed.hour * 60 + parsed.minute


def clock_label(minutes: int) -> str:
    hour, minute = divmod(minutes, 60)
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def read_block(block: dict) -> Meeting:
    days = block["days"]
    if not re.fullmatch(r"[MTWRFSU]+", days):
        raise ValueError("invalid weekdays")
    times = []
    for key in ("start", "end"):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", block[key]):
            raise ValueError("invalid clock time")
        hour, minute = map(int, block[key].split(":"))
        times.append(hour * 60 + minute)
    if times[1] <= times[0]:
        raise ValueError("end before start")
    return Meeting(frozenset(DAY_CODES.index(day) for day in days), *times)


def load_schedule(value: str) -> Schedule:
    if not value:
        return Schedule()
    try:
        config = json.loads(value)
        if not isinstance(config, dict) or set(config) - {"hard", "soft"}:
            raise ValueError("invalid schedule structure")
        if any(not isinstance(config.get(key, []), list) for key in ("hard", "soft")):
            raise ValueError("invalid block list")
        return Schedule(tuple(read_block(block) for block in config.get("hard", [])),
                        tuple(read_block(block) for block in config.get("soft", [])))
    except (KeyError, TypeError, ValueError) as error:
        raise WatchError("SEAT_WATCH_SCHEDULE must contain hard/soft arrays of days, start and end blocks.") from error


def read_days(text: str) -> frozenset[int]:
    names = frozenset(day for day, name in enumerate(DAY_NAMES) if re.search(rf"\b{name}\b", text, re.I))
    if names:
        return names
    abbreviations = {"m": 0, "mon": 0, "t": 1, "tu": 1, "tue": 1,
                     "w": 2, "wed": 2, "r": 3, "th": 3, "thu": 3,
                     "f": 4, "fri": 4, "s": 5, "sat": 5, "u": 6, "sun": 6}
    tokens = re.findall(r"\b[A-Za-z]+\b", text)
    if len(tokens) == 1 and re.fullmatch(r"[MTWRFSU]+", tokens[0]):
        return frozenset(DAY_CODES.index(day) for day in tokens[0])
    return frozenset(abbreviations[token.lower()] for token in tokens if token.lower() in abbreviations)


def read_meetings(text: str, campus: str) -> tuple[tuple[Meeting, ...], bool]:
    blocks = re.split(r"\bClass\s+on\s*:", normalize(text), flags=re.I)
    blocks = blocks[1:] if len(blocks) > 1 else blocks
    meetings = []
    unknown = False
    for block in blocks:
        time = TIME_RANGE.search(block)
        if not time:
            if campus != CAMPUSES[1] or not re.match(r"\s*(?:None|TBA)\b", block, re.I):
                unknown = True
            continue
        days = read_days(block[:time.start()])
        start, end = (clock_minutes(value) for value in time.groups())
        if not days or end <= start:
            unknown = True
            continue
        meeting = Meeting(days, start, end)
        if meeting not in meetings:
            meetings.append(meeting)
    return tuple(meetings), unknown


def parse_section(row: dict[str, str]) -> Section:
    course = f"{normalize(row['subject'])} {normalize(row['courseNumber'])}"
    crn = normalize(row["courseReferenceNumber"])
    campus = normalize(row["campus"])
    sequence = normalize(row["sequenceNumber"])
    if course not in COURSES or campus not in CAMPUSES or not crn.isdigit() or not sequence:
        raise WatchError(f"Unexpected result row for {course}, CRN {crn!r}, campus {campus!r}.")
    seats, capacity = read_seats(row["status"])
    meetings, unknown = read_meetings(row["meetingTime"], campus)
    return Section(course, sequence, crn, normalize(row["courseTitle"]), campus,
                   seats, capacity, meetings, unknown)


def choose_select2(page, selector: str, query: str, option: str | re.Pattern) -> None:
    field = page.locator(selector)
    field.fill(query)
    page.get_by_role("option", name=option, exact=isinstance(option, str)).click()


def select2_selector(page, container: str, fallback: str) -> str:
    stable = f"#{container} input.select2-input"
    return stable if page.locator(stable).count() == 1 else fallback


def selected_chips(page, selector: str) -> list[str]:
    container = page.locator(selector).locator("xpath=ancestor::ul[1]")
    return [normalize(value) for value in container.locator(".select2-search-choice").all_inner_texts()]


def read_banner(page) -> list[Section]:
    page.goto(BANNER_URL, wait_until="domcontentloaded")
    continue_button = page.get_by_role("button", name="Continue", exact=True)
    continue_button.wait_for()
    if not page.get_by_role("link", name=TERM, exact=True).is_visible():
        page.get_by_role("link", name="Select a term...", exact=True).click()
        page.get_by_role("combobox", name="Select a Term for Class Search").fill(TERM)
        page.get_by_role("option", name=TERM, exact=True).click()
    page.get_by_role("link", name=TERM, exact=True).wait_for()
    continue_button.click()
    page.get_by_role("button", name=re.compile(r"^Advanced Search")).click()

    course_selector = select2_selector(page, "s2id_txt_subjectcoursecombo", "#s2id_autogen3")
    for course in COURSES:
        prefix = course.replace(" ", "")
        choose_select2(page, course_selector, course, re.compile(rf"^{prefix}\b"))
    selected_courses = selected_chips(page, course_selector)
    if len(selected_courses) != len(COURSES) or any(
        not any(re.match(rf"^{course.replace(' ', '')}\b", chip) for chip in selected_courses)
        for course in COURSES
    ):
        raise WatchError(f"Could not verify all course filters in Banner: {selected_courses!r}.")

    campus_selector = select2_selector(page, "s2id_txt_campus", "#s2id_autogen5")
    choose_select2(page, campus_selector, "Main Campus", CAMPUSES[0])
    choose_select2(page, campus_selector, "Online - Main Campus", CAMPUSES[1])
    if set(selected_chips(page, campus_selector)) != set(CAMPUSES):
        raise WatchError("Could not verify both campus filters in Banner.")

    open_only = page.get_by_role("checkbox", name="Open Sections Only")
    if open_only.is_checked():
        open_only.uncheck()
    page.get_by_role("button", name="Search", exact=True).click()
    heading = page.get_by_role("heading", name=RESULT_HEADING)
    count_text = heading.inner_text()
    count_match = RESULT_HEADING.search(count_text)
    if not count_match:
        raise WatchError(f"Could not read Banner's result count: {count_text!r}.")
    total = int(count_match.group(1).replace(",", ""))
    if total == 0:
        return []
    if total > 50:
        raise WatchError(f"Banner returned {total} rows, above the supported 50-row page size.")
    if total > 10:
        page.get_by_role("combobox", name="10 per page", exact=True).select_option("50")
        page.get_by_role("combobox", name="50 per page", exact=True).wait_for()

    table = page.locator("#table1")
    table.locator(f"td[data-property='courseReferenceNumber']").nth(total - 1).wait_for()
    rows = table.locator("tbody tr").filter(has=page.locator("td[data-property='courseReferenceNumber']"))
    entries = rows.evaluate_all(
        """(rows, fields) => rows.map(row => Object.fromEntries(fields.map(field => {
            const cell = row.querySelector(`td[data-property="${field}"]`);
            if (!cell) throw new Error(`Missing Banner cell ${field}`);
            return [field, cell.innerText];
        })))""",
        FIELDS,
    )
    sections = [parse_section(entry) for entry in entries]
    if len(sections) != total or len({section.crn for section in sections}) != total:
        raise WatchError(f"Banner reported {total} results but returned {len(sections)} rows or duplicate CRNs.")
    return sections


def search_sections() -> list[Section]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            page.set_default_timeout(25_000)
            return read_banner(page)
        finally:
            browser.close()


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["version"] != STATE_VERSION or state["term"] != TERM:
            raise ValueError("wrong term or state version")
        if not all(isinstance(state[key], list) and all(isinstance(crn, str) for crn in state[key])
                   for key in ("seen", "open")):
            raise ValueError("invalid CRN lists")
        if not set(state["open"]).issubset(state["seen"]):
            raise ValueError("open CRNs missing from seen list")
        return state
    except (KeyError, TypeError, ValueError) as error:
        raise WatchError(f"Could not load {path}: {error}.") from error


def compare_sections(sections: list[Section], previous: dict | None,
                     notify_initial: bool, checked_at: datetime, schedule: Schedule) -> tuple[dict, list[str]]:
    old_seen = set(previous["seen"]) if previous is not None else set()
    old_open = set(previous["open"]) if previous is not None else set()
    eligible = [section for section in sections if section.eligibility(schedule) in ("open", "swap")]
    current = {
        "version": STATE_VERSION,
        "term": TERM,
        "checked_at": checked_at.isoformat(),
        "seen": sorted(old_seen | {section.crn for section in sections}),
        "open": sorted(section.crn for section in eligible),
    }
    alerts = []
    if previous is not None or notify_initial:
        for section in eligible:
            if section.crn not in old_open:
                label = ("Open at baseline" if previous is None else
                         "New section" if section.crn not in old_seen else "Seat now available")
                alerts.append(f"{label}: {section.label(schedule)}")
    return current, alerts


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(state, temporary, indent=2)
            temporary.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def send_notification(title: str, message: str) -> None:
    topic = os.environ.get("NTFY_TOPIC", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        raise WatchError("Set NTFY_TOPIC to your ntfy topic before sending alerts.")
    url = (os.environ.get("NTFY_URL") or "https://ntfy.sh").rstrip("/")
    payload = {"topic": topic, "title": title, "message": message, "priority": 4,
               "tags": ["mortar_board"], "click": BANNER_URL}
    headers = {"Content-Type": "application/json"}
    if token := os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urlopen(request, timeout=20) as response:
        if not 200 <= response.status < 300:
            raise WatchError(f"ntfy returned HTTP {response.status}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_state = os.environ.get("SEAT_WATCH_STATE", Path(__file__).with_name("state.json"))
    parser.add_argument("--state", type=Path, default=default_state, help="Path to the snapshot JSON.")
    parser.add_argument("--notify-initial", action="store_true", help="Alert on open seats on the first check.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="Read Banner, print results, skip sends and state writes.")
    modes.add_argument("--test-notification", "--test-push", action="store_true", help="Send a test push without checking Banner.")
    args = parser.parse_args(argv)
    try:
        if args.test_notification:
            send_notification("UA seat watcher test", "This is a test push. No Banner section was checked.")
            print("Test push sent.")
            return 0
        now = datetime.now(CENTRAL)
        if now.date() > LAST_CHECK_DATE:
            print(f"Monitoring ended after {LAST_CHECK_DATE.isoformat()} Central.")
            return 0
        schedule = load_schedule(os.environ.get("SEAT_WATCH_SCHEDULE", ""))
        previous = load_state(args.state)
        sections = search_sections()
        current, alerts = compare_sections(sections, previous, args.notify_initial, now, schedule)
        print(f"{now.isoformat()}: {len(sections)} sections checked, {len(current['open'])} with eligible seats.")
        if args.dry_run:
            for section in sections:
                if section.eligibility(schedule) in ("open", "swap"):
                    print(section.label(schedule))
            print(f"{len(alerts)} alerts would be sent. State left unchanged.")
            return 0
        if alerts:
            send_notification("UA Spring 2027 seat update", "\n".join(alerts))
            print(f"Sent one notification for {len(alerts)} section(s).")
        if previous is None or any(current[key] != previous[key] for key in ("seen", "open")):
            save_state(args.state, current)
        return 0
    except Exception as error:
        print(f"Seat watch failed. Previous state was kept. {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
