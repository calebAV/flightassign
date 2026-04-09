#!/usr/bin/env python3
"""
FlightAssign — ATL Rolling Flight Assignment Automation
Runs every 15 minutes on weekdays via GitHub Actions.
Fetches flights from Fleet API, assigns operators, posts to Slack.
"""

import os
import re
import json
import requests
from datetime import datetime, timezone, timedelta

# ─── Config ───────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C0AQEA7NR28")
FLEET_API_URL = os.environ.get("FLEET_API_URL", "https://beta.api.fleet.aerovect.com/flights?airport=ATL")

EDT = timezone(timedelta(hours=-4))
NOW = datetime.now(EDT)

# Shift definitions
SHIFT1_START = "5:30 AM"
SHIFT1_END = "2:00 PM"
SHIFT2_START = "2:00 PM"
SHIFT2_END = "10:00 PM"

SHIFT1_BREAKS = [("7:30 AM", "8:30 AM"), ("9:30 AM", "10:30 AM"), ("11:00 AM", "12:00 PM")]
SHIFT2_BREAKS = [("4:00 PM", "5:00 PM"), ("5:30 PM", "6:30 PM"), ("7:00 PM", "8:00 PM")]

# Gate filter: T gates + A01–A18 only
GATE_PATTERN = re.compile(r"^(T\d+[A-Z]?|A(0[1-9]|1[0-8])[A-Z]?)$")


# ─── Helpers ──────────────────────────────────────────────────────────────────
def parse_time(t_str):
    """Parse time string like '4:00 PM' into datetime for today in EDT."""
    return datetime.strptime(
        f"{NOW.strftime('%Y-%m-%d')} {t_str}", "%Y-%m-%d %I:%M %p"
    ).replace(tzinfo=EDT)


def fmt_time(dt):
    """Format datetime as '2:39 PM'."""
    return dt.strftime("%-I:%M %p")


def pier_display(pier_val):
    """Show pier only if value is 40–60, otherwise 'Pier N/A'."""
    try:
        p = int(pier_val)
        if 40 <= p <= 60:
            return f"Pier {p}"
    except (ValueError, TypeError):
        pass
    return "Pier N/A"


def slack_api(method, **kwargs):
    """Call a Slack Web API method."""
    resp = requests.post(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json=kwargs,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error ({method}): {data.get('error')}")
    return data


# ─── Step 1: Load roster from Slack ──────────────────────────────────────────
def load_roster():
    """
    Read recent messages from the channel to find the most recent
    WEEKLY_SCHEDULE_JSON and the most recent schedule post for change detection.
    """
    roster = None
    previous_flights = {}
    cursor = None

    # Page through up to 200 messages looking for the roster
    for _ in range(5):
        params = {"channel": SLACK_CHANNEL_ID, "limit": 40}
        if cursor:
            params["cursor"] = cursor
        data = slack_api("conversations.history", **params)

        for msg in data.get("messages", []):
            text = msg.get("text", "")

            # Look for weekly schedule JSON
            if roster is None and "WEEKLY_SCHEDULE_JSON" in text:
                # Extract JSON from code block
                json_match = re.search(r"```(\{.*?\})```", text, re.DOTALL)
                if not json_match:
                    json_match = re.search(r"```json\s*(\{.*?\})```", text, re.DOTALL)
                if json_match:
                    roster = json.loads(json_match.group(1))

            # Look for previous schedule post (for change detection)
            if not previous_flights and "ATL Flight Assignments" in text:
                # Parse flight → gate mappings from previous post
                for line in text.split("\n"):
                    m = re.search(
                        r"dept — _([A-Z]{2}\d+)_ → \w+ \| Gate (\S+)", line
                    )
                    if m:
                        previous_flights[m.group(1)] = {"gate": m.group(2)}

        cursor = (
            data.get("response_metadata", {}).get("next_cursor")
        )
        if not cursor or roster:
            break

    if roster is None:
        raise RuntimeError("Could not find WEEKLY_SCHEDULE_JSON in channel history")

    return roster, previous_flights


def get_todays_operators(roster):
    """
    Extract today's shift operators from the roster JSON.
    Returns (shift_number, operators_list).
    """
    day_name = NOW.strftime("%A").lower()
    days = roster.get("days", {})
    day_data = days.get(day_name)

    if not day_data:
        return None, []

    # Determine which shift we're in
    shift1_end_dt = parse_time(SHIFT1_END)
    shift2_end_dt = parse_time(SHIFT2_END)
    shift1_start_dt = parse_time(SHIFT1_START)

    if NOW < shift1_start_dt or NOW >= shift2_end_dt:
        return None, []  # Outside shift hours

    if NOW < shift1_end_dt:
        shift_num = 1
        shift_key = "shift1"
        breaks = SHIFT1_BREAKS
    else:
        shift_num = 2
        shift_key = "shift2"
        breaks = SHIFT2_BREAKS

    raw_ops = day_data.get(shift_key, [])
    operators = []
    for i, op in enumerate(raw_ops):
        # Skip Route 1 operators
        role = op.get("role", "")
        if "route 1" in role.lower():
            continue

        # Assign staggered breaks
        break_idx = min(i, len(breaks) - 1)
        b_start, b_end = breaks[break_idx]

        operators.append({
            "name": op["name"],
            "role": role,
            "break_start": b_start,
            "break_end": b_end,
            "break_start_dt": parse_time(b_start),
            "break_end_dt": parse_time(b_end),
            "next_avail": parse_time(SHIFT1_START if shift_num == 1 else SHIFT2_START),
            "flights": [],
            "flight_count": 0,
        })

    return shift_num, operators


# ─── Step 2: Fetch flights ───────────────────────────────────────────────────
def fetch_flights():
    """Fetch flights from Fleet API and filter to in-scope gates."""
    resp = requests.get(FLEET_API_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    outbound = data.get("outbound", [])
    total_outbound = len(outbound)

    inscope = []
    for f in outbound:
        gate = f.get("dptr_gate") or ""
        if f.get("cncl_ind") == "Y":
            continue
        if GATE_PATTERN.match(gate):
            inscope.append({
                "al_cde": f.get("al_cde"),
                "flt_num": f.get("flt_num"),
                "dest": f.get("leg_dest_ap_cde"),
                "mission_time": f.get("mission_time"),
                "gate": gate,
                "pier": f.get("dptr_bag_pier_num"),
                "time_type": f.get("time_type"),
            })

    inscope.sort(key=lambda x: x["mission_time"])
    return inscope, total_outbound


# ─── Step 3: Assignment engine ───────────────────────────────────────────────
def run_assignments(flights, operators, previous_flights):
    """
    Assign flights to operators using round-robin with constraints:
    - Haulout = departure - 50 min (contractual, never adjust)
    - Only future haulout times
    - 20-min spacing between consecutive haulouts per operator
    - No haulouts at or after 10 PM
    - Operators cannot start haulout during break window
    """
    cutoff_10pm = parse_time("10:00 PM")

    # Compute haulout times and filter to actionable
    actionable = []
    for fl in flights:
        dept_dt = datetime.fromtimestamp(fl["mission_time"] / 1000, tz=EDT)
        haulout_dt = dept_dt - timedelta(minutes=50)
        fl["dept_dt"] = dept_dt
        fl["haulout_dt"] = haulout_dt
        if haulout_dt > NOW:
            actionable.append(fl)

    actionable.sort(key=lambda x: x["haulout_dt"])
    unassigned = []

    for fl in actionable:
        haulout = fl["haulout_dt"]

        # Hard constraint: no haulout at or after 10 PM
        if haulout >= cutoff_10pm:
            unassigned.append(fl)
            continue

        # Find available operators
        candidates = []
        for op in operators:
            if op["next_avail"] > haulout:
                continue
            if op["break_start_dt"] <= haulout < op["break_end_dt"]:
                continue
            candidates.append(op)

        if not candidates:
            unassigned.append(fl)
            continue

        # Round-robin: fewer flights first, then earliest available
        candidates.sort(key=lambda o: (o["flight_count"], o["next_avail"]))
        chosen = candidates[0]

        chosen["flights"].append(fl)
        chosen["flight_count"] += 1
        chosen["next_avail"] = haulout + timedelta(minutes=20)

        # If next_avail falls during break, push to break end
        if chosen["break_start_dt"] <= chosen["next_avail"] < chosen["break_end_dt"]:
            chosen["next_avail"] = chosen["break_end_dt"]

    return actionable, unassigned


def detect_change(fl, previous_flights):
    """Check if a flight's gate changed vs the previous post."""
    flt_key = f"{fl['al_cde']}{fl['flt_num']}"
    if flt_key not in previous_flights:
        return False, None
    prev = previous_flights[flt_key]
    if prev["gate"] != fl["gate"]:
        return True, f"was {prev['gate']}"
    return False, None


# ─── Step 4: Verify constraints ──────────────────────────────────────────────
def verify(operators, cutoff_10pm):
    """Pre-post verification. Raises on failure."""
    errors = []
    for op in operators:
        prev_h = None
        for fl in op["flights"]:
            expected_h = fl["dept_dt"] - timedelta(minutes=50)
            if fl["haulout_dt"] != expected_h:
                errors.append(f"Haulout mismatch: {op['name']} {fl['al_cde']}{fl['flt_num']}")
            if fl["haulout_dt"] <= NOW:
                errors.append(f"Past haulout: {op['name']} {fl['al_cde']}{fl['flt_num']}")
            if prev_h and (fl["haulout_dt"] - prev_h).total_seconds() < 1200:
                errors.append(f"Spacing <20m: {op['name']} {fmt_time(prev_h)}→{fmt_time(fl['haulout_dt'])}")
            if fl["haulout_dt"] >= cutoff_10pm:
                errors.append(f">=10PM haulout: {op['name']} {fl['al_cde']}{fl['flt_num']}")
            prev_h = fl["haulout_dt"]
    if errors:
        raise RuntimeError("Verification failed:\n" + "\n".join(errors))


# ─── Step 5: Format Slack message ────────────────────────────────────────────
def format_message(shift_num, operators, actionable, unassigned, total_inscope, total_outbound, previous_flights):
    """Build the Slack message string."""
    day_str = NOW.strftime("%A %-m/%-d")
    clock_num = int(NOW.strftime("%I"))
    snapshot_time = NOW.strftime("%-I:%M %p")

    lines = [
        f":airplane: *ATL Flight Assignments — {day_str}*",
        f":clock{clock_num}: _Shift {shift_num} schedule ({snapshot_time} EDT snapshot)_",
    ]

    def flight_line(fl):
        flt_name = f"{fl['al_cde']}{fl['flt_num']}"
        h = fmt_time(fl["haulout_dt"])
        d = fmt_time(fl["dept_dt"])
        changed, note = detect_change(fl, previous_flights)
        gate_str = f"Gate {fl['gate']}"
        if note:
            gate_str += f" ({note})"
        p = pier_display(fl.get("pier"))
        prefix = ":warning: " if changed else ""
        return f"\u2022 {prefix}{h} haulout / {d} dept \u2014 _{flt_name}_ \u2192 {fl['dest']} | {gate_str} | {p}"

    for op in operators:
        lines.append("\u200e")  # Left-to-right mark for spacing
        name_upper = op["name"].upper()
        lines.append(
            f"*:bust_in_silhouette: {name_upper}* \u2014 {op['role']} | Break: {op['break_start']}\u2013{op['break_end']}"
        )
        if not op["flights"]:
            lines.append("_No upcoming flights_")
        else:
            for fl in op["flights"]:
                lines.append(flight_line(fl))

    if unassigned:
        lines.append("\u200e")
        lines.append("_Unassigned flights:_")
        for fl in unassigned:
            lines.append(flight_line(fl))

    lines.append("\u200e")
    total_assigned = sum(len(op["flights"]) for op in operators)
    lines.append(
        f"_{total_assigned} assigned | {len(unassigned)} unassigned of {len(actionable)} actionable T/A-gate flights_"
    )
    lines.append(
        f"_Fleet API: {total_outbound} outbound flights total | {total_inscope} on in-scope gates (T + A01\u2013A18)_"
    )

    if shift_num == 1:
        s2_names = []  # Would need to look up shift2 roster
        lines.append(f"_Shift 2 starts at 2:00 PM_")
    else:
        lines.append("_Shift 1 ended at 2:00 PM_")

    return "\n".join(lines)


# ─── Step 6: Post to Slack ───────────────────────────────────────────────────
def post_to_slack(message):
    """Post as a new top-level message. Split if >3800 chars."""
    if len(message) <= 3800:
        slack_api("chat.postMessage", channel=SLACK_CHANNEL_ID, text=message)
    else:
        # Split at the last operator section boundary before 3800
        split_idx = message.rfind("\u200e", 0, 3800)
        if split_idx == -1:
            split_idx = 3800
        part1 = message[:split_idx]
        part2 = message[split_idx:]
        slack_api("chat.postMessage", channel=SLACK_CHANNEL_ID, text=part1)
        slack_api("chat.postMessage", channel=SLACK_CHANNEL_ID, text=part2)


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    print(f"FlightAssign run at {fmt_time(NOW)} EDT on {NOW.strftime('%A %Y-%m-%d')}")

    # Check shift hours
    shift1_start = parse_time(SHIFT1_START)
    shift2_end = parse_time(SHIFT2_END)

    if NOW < shift1_start or NOW >= shift2_end:
        msg = (
            f":airplane: _ATL Flight Assignments \u2014 {NOW.strftime('%A %-m/%-d')}_\n"
            f":clock{int(NOW.strftime('%I'))}: _Outside shift hours ({fmt_time(NOW)} EDT)_\n"
            "All shifts have ended for today. Shift 1 resumes next business day at 5:30 AM EDT.\n"
            "_No active assignments \u2014 operations resume next business day._"
        )
        post_to_slack(msg)
        print("Outside shift hours. Posted notice.")
        return

    try:
        # Step 1: Load roster
        print("Loading roster from Slack...")
        roster, previous_flights = load_roster()

        shift_num, operators = get_todays_operators(roster)
        if shift_num is None or not operators:
            print("No operators for current shift. Skipping.")
            return

        print(f"Shift {shift_num} with {len(operators)} operators")

        # Step 2: Fetch flights
        print("Fetching flights from Fleet API...")
        inscope_flights, total_outbound = fetch_flights()
        print(f"{total_outbound} total outbound, {len(inscope_flights)} in-scope")

        # Step 3: Run assignments
        print("Running assignment engine...")
        actionable, unassigned = run_assignments(inscope_flights, operators, previous_flights)
        total_assigned = sum(len(op["flights"]) for op in operators)
        print(f"{total_assigned} assigned, {len(unassigned)} unassigned of {len(actionable)} actionable")

        # Step 4: Verify
        print("Verifying constraints...")
        verify(operators, parse_time("10:00 PM"))
        print("All checks passed!")

        # Step 5: Format
        message = format_message(
            shift_num, operators, actionable, unassigned,
            len(inscope_flights), total_outbound, previous_flights
        )

        # Step 6: Post
        print(f"Posting to Slack ({len(message)} chars)...")
        post_to_slack(message)
        print("Done!")

    except requests.RequestException as e:
        # Fleet API unreachable
        error_msg = (
            f":x: ATL Assignment Check \u2014 {fmt_time(NOW)} EDT \u2014 "
            f"Fleet API unavailable. Will retry next cycle.\n_{e}_"
        )
        post_to_slack(error_msg)
        print(f"Fleet API error: {e}")
        raise

    except Exception as e:
        print(f"Error: {e}")
        raise


if __name__ == "__main__":
    main()
