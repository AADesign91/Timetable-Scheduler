from flask import Flask, render_template, request
import json
import math

app = Flask(__name__)

# Base time-slot length (minutes)
TIME_SLOT_MINUTES = 10


# ---------- TIME HELPERS ----------

def parse_time_str(t: str):
    """Parse 'HH:MM' into (hour, minute)."""
    parts = (t or "").split(":")
    if len(parts) != 2:
        return 8, 0
    try:
        h = int(parts[0])
        m = int(parts[1])
        return h, m
    except ValueError:
        return 8, 0


def time_to_minutes(h: int, m: int) -> int:
    return h * 60 + m


def minutes_to_str(total_minutes: int) -> str:
    h = total_minutes // 60
    m = total_minutes % 60
    return f"{h:02d}:{m:02d}"


def build_time_slots(workday_start: str, workday_end: str, blackouts):
    """
    Build list of slot start times between workday_start and workday_end,
    in TIME_SLOT_MINUTES increments, excluding blackout windows.
    """
    start_h, start_m = parse_time_str(workday_start)
    end_h, end_m = parse_time_str(workday_end)

    start_minutes = time_to_minutes(start_h, start_m)
    end_minutes = time_to_minutes(end_h, end_m)

    # Safety fallback if invalid
    if end_minutes <= start_minutes:
        start_minutes = time_to_minutes(8, 0)
        end_minutes = time_to_minutes(17, 0)

    blackout_ranges = []
    for b in blackouts or []:
        s = b.get("start")
        e = b.get("end")
        if not s or not e:
            continue
        sh, sm = parse_time_str(s)
        eh, em = parse_time_str(e)
        sb = time_to_minutes(sh, sm)
        eb = time_to_minutes(eh, em)
        if eb > sb:
            blackout_ranges.append((sb, eb))

    def in_blackout(minute_val: int) -> bool:
        for sb, eb in blackout_ranges:
            if sb <= minute_val < eb:
                return True
        return False

    slots = []
    current = start_minutes
    while current < end_minutes:
        if not in_blackout(current):
            slots.append(minutes_to_str(current))
        current += TIME_SLOT_MINUTES
    return slots


# ---------- CORE SCHEDULER ----------

def run_scheduler(payload):
    """
    Expected payload from front end:

    {
      "cycle_length": 5,
      "workday_start": "08:00",
      "workday_end": "17:00",
      "max_clients_per_slot": 4,
      "blackouts": [
        {"start": "12:00", "end": "13:00"},
        ...
      ],
      "clients": [
        {
          "name": "Nora",
          "sessions_needed": 3,
          "session_length_minutes": 60,
          "tag": "ELL",
          "spacing_rule": "none" | "once_per_day" | "no_consecutive_days",
          "max_per_day": 2,  # optional
          "group_id": "G1" or "",
          "availability": {
            "Day1": ["08:00", "08:10", ...],
            ...
          }
        },
        ...
      ]
    }
    """
    clients = payload.get("clients", [])
    cycle_length = int(payload.get("cycle_length", 6) or 6)
    if cycle_length < 1:
        cycle_length = 1
    if cycle_length > 20:
        cycle_length = 20

    workday_start = payload.get("workday_start", "08:00") or "08:00"
    workday_end = payload.get("workday_end", "17:00") or "17:00"

    max_clients_per_slot = int(payload.get("max_clients_per_slot", 1) or 1)
    if max_clients_per_slot < 1:
        max_clients_per_slot = 1
    if max_clients_per_slot > 50:
        max_clients_per_slot = 50

    blackouts = payload.get("blackouts") or []

    days = [f"Day{d}" for d in range(1, cycle_length + 1)]
    time_slots = build_time_slots(workday_start, workday_end, blackouts)

    # Each slot holds a LIST of client names
    timetable = {day: {slot: [] for slot in time_slots} for day in days}

    # Colour classes per client
    color_classes = [
        "color-1", "color-2", "color-3", "color-4",
        "color-5", "color-6", "color-7", "color-8"
    ]
    student_colors = {}
    for idx, client in enumerate(clients):
        name = (client.get("name") or "").strip()
        if not name:
            continue
        student_colors[name] = color_classes[idx % len(color_classes)]

    conflicts = []
    summary = {}

    # Grouping
    groups = {}
    solo_clients = []

    for client in clients:
        name = (client.get("name") or "").strip()
        if not name:
            continue
        group_id = client.get("group_id") or ""
        if group_id:
            groups.setdefault(group_id, []).append(client)
        else:
            solo_clients.append(client)

    def normalize_availability(client):
        availability = client.get("availability", {}) or {}
        normalized = {}
        for day_key, times in availability.items():
            if isinstance(day_key, str) and day_key.startswith("Day"):
                day = day_key
            else:
                day = f"Day{day_key}"
            normalized.setdefault(day, [])
            normalized[day].extend(times)
        return {day: set(times) for day, times in normalized.items()}

    def analyze_failure_reason(time_slots, days, avail_by_day, slots_per_session):
        """
        Simple "smart suggestions" explanation.
        """
        has_any_block = False
        for day in days:
            day_avail = avail_by_day.get(day, set())
            if not day_avail:
                continue
            for start_idx in range(len(time_slots) - slots_per_session + 1):
                block = time_slots[start_idx:start_idx + slots_per_session]
                if all(slot in day_avail for slot in block):
                    has_any_block = True
                    break
            if has_any_block:
                break

        if not has_any_block:
            return "Not enough contiguous availability to fit full sessions."
        return "Available blocks exist but are filled or blocked by spacing/capacity rules."

    # ---------- Schedule groups first ----------
    for group_id, members in groups.items():
        if not members:
            continue

        template = members[0]
        sessions_needed = int(template.get("sessions_needed", 0) or 0)
        if sessions_needed <= 0:
            continue

        session_length = int(
            template.get("session_length_minutes") or TIME_SLOT_MINUTES
        )
        if session_length <= 0:
            session_length = TIME_SLOT_MINUTES

        slots_per_session = max(1, math.ceil(session_length / TIME_SLOT_MINUTES))
        spacing_rule = template.get("spacing_rule", "none")

        # Combined availability = intersection of all members
        avail_by_day_list = [normalize_availability(m) for m in members]
        combined_avail_by_day = {}
        for day in days:
            if not avail_by_day_list:
                continue
            base = avail_by_day_list[0].get(day, set()).copy()
            for other in avail_by_day_list[1:]:
                base &= other.get(day, set())
            if base:
                combined_avail_by_day[day] = base

        scheduled_count = 0
        used_days_for_group = set()
        used_day_indices = []
        group_size = len(members)
        reason = ""

        for _ in range(sessions_needed):
            placed_this_session = False

            for day_index, day in enumerate(days):
                # Spacing rules
                if spacing_rule == "once_per_day" and day in used_days_for_group:
                    continue
                if spacing_rule == "no_consecutive_days":
                    if any(abs(day_index - idx) <= 1 for idx in used_day_indices):
                        continue

                day_avail = combined_avail_by_day.get(day, set())
                if not day_avail:
                    continue

                for start_idx in range(len(time_slots) - slots_per_session + 1):
                    block = time_slots[start_idx:start_idx + slots_per_session]
                    ok = True
                    for slot in block:
                        if slot not in day_avail:
                            ok = False
                            break
                        if len(timetable[day][slot]) + group_size > max_clients_per_slot:
                            ok = False
                            break
                    if ok:
                        # Book for all group members
                        for slot in block:
                            for m in members:
                                nm = (m.get("name") or "").strip()
                                if nm and nm not in timetable[day][slot]:
                                    timetable[day][slot].append(nm)
                        scheduled_count += 1
                        used_days_for_group.add(day)
                        used_day_indices.append(day_index)
                        placed_this_session = True
                        break
                if placed_this_session:
                    break

            if not placed_this_session:
                reason = analyze_failure_reason(
                    time_slots, days, combined_avail_by_day, slots_per_session
                )
                break

        member_names = [
            (m.get("name") or "").strip()
            for m in members
            if (m.get("name") or "").strip()
        ]

        if scheduled_count < sessions_needed:
            msg = (
                f"Unable to fully schedule group {group_id} "
                f"({', '.join(member_names)}): "
                f"needed {sessions_needed}, scheduled {scheduled_count}."
            )
            if reason:
                msg += f" Reason: {reason}"
            conflicts.append(msg)

        for m in members:
            m_name = (m.get("name") or "").strip()
            if not m_name:
                continue
            summary[m_name] = {
                "needed": sessions_needed,
                "scheduled": scheduled_count,
                "session_length": session_length,
                "reason": reason if scheduled_count < sessions_needed else "",
            }

    # ---------- Schedule solo clients ----------
    for client in solo_clients:
        name = (client.get("name") or "").strip()
        if not name:
            continue

        sessions_needed = int(client.get("sessions_needed", 0) or 0)
        if sessions_needed <= 0:
            continue

        session_length = int(
            client.get("session_length_minutes") or TIME_SLOT_MINUTES
        )
        if session_length <= 0:
            session_length = TIME_SLOT_MINUTES

        slots_per_session = max(1, math.ceil(session_length / TIME_SLOT_MINUTES))
        spacing_rule = client.get("spacing_rule", "none")

        max_per_day_raw = client.get("max_per_day")
        max_per_day = None
        if max_per_day_raw not in (None, "", 0, "0"):
            try:
                max_per_day = int(max_per_day_raw)
                if max_per_day < 1:
                    max_per_day = None
            except (TypeError, ValueError):
                max_per_day = None

        avail_by_day = normalize_availability(client)

        scheduled_count = 0
        used_days_for_client = set()
        used_day_indices = []
        per_day_counts = {day: 0 for day in days}
        reason = ""

        for _ in range(sessions_needed):
            placed_this_session = False

            for day_index, day in enumerate(days):
                # Spacing rules
                if spacing_rule == "once_per_day" and day in used_days_for_client:
                    continue
                if spacing_rule == "no_consecutive_days":
                    if any(abs(day_index - idx) <= 1 for idx in used_day_indices):
                        continue
                if max_per_day is not None and per_day_counts[day] >= max_per_day:
                    continue

                day_avail = avail_by_day.get(day, set())
                if not day_avail:
                    continue

                for start_idx in range(len(time_slots) - slots_per_session + 1):
                    block = time_slots[start_idx:start_idx + slots_per_session]
                    ok = True
                    for slot in block:
                        if slot not in day_avail:
                            ok = False
                            break
                        if len(timetable[day][slot]) + 1 > max_clients_per_slot:
                            ok = False
                            break
                    if ok:
                        for slot in block:
                            if name not in timetable[day][slot]:
                                timetable[day][slot].append(name)
                        scheduled_count += 1
                        used_days_for_client.add(day)
                        used_day_indices.append(day_index)
                        per_day_counts[day] += 1
                        placed_this_session = True
                        break
                if placed_this_session:
                    break

            if not placed_this_session:
                reason = analyze_failure_reason(
                    time_slots, days, avail_by_day, slots_per_session
                )
                break

        if scheduled_count < sessions_needed:
            msg = (
                f"Unable to fully schedule {name}: "
                f"needed {sessions_needed}, scheduled {scheduled_count}."
            )
            if reason:
                msg += f" Reason: {reason}"
            conflicts.append(msg)

        summary[name] = {
            "needed": sessions_needed,
            "scheduled": scheduled_count,
            "session_length": session_length,
            "reason": reason if scheduled_count < sessions_needed else "",
        }

    return {
        "days": days,
        "time_slots": time_slots,
        "timetable": timetable,
        "conflicts": conflicts,
        "summary": summary,
        "student_colors": student_colors,
    }


# ---------- ROUTES ----------

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/generate_timetable", methods=["POST"])
def generate_timetable():
    """
    Universal POST handler — works on both Replit and Render.
    Accepts:
      - JSON (Content-Type: application/json)  ← used by fetch() and Render
      - form-data containing payload_json       ← used by Replit <form>
    """

    # 1. Try JSON body (Render)
    data = request.get_json(silent=True)

    # 2. Fallback: form post (Replit)
    if not data:
        raw = request.form.get("payload_json")
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return "Invalid JSON in payload_json", 400

    # 3. If still nothing, reject
    if not data:
        return "Missing timetable data", 400

    # 4. Run scheduling engine
    result = run_scheduler(data)

    # 5. Render result
    return render_template(
        "timetable.html",
        days=result["days"],
        time_slots=result["time_slots"],
        timetable=result["timetable"],
        conflicts=result["conflicts"],
        summary=result["summary"],
        student_colors=result["student_colors"],
    )



if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8000)
