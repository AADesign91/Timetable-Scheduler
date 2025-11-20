from flask import Flask, render_template, request, jsonify
from collections import defaultdict
import itertools

app = Flask(__name__)


# ---------- TIME TEMPLATE HELPERS ----------

def build_time_slots(template_name: str):
    """
    Return a list of time strings based on a named schedule template.
    Currently supports:
      - 'campus_a' : 30-min slots 08:00–16:00 (skip 12:00–13:00 for lunch)
      - 'campus_b' : 8–8:30, 8:30–9:00, then 40-min blocks with recess gaps
    """
    slots = []

    if template_name == "campus_b":
        # 8:00–8:30 and 8:30–9:00 (two 30-min pre-period chunks)
        slots.append("08:00")
        slots.append("08:30")

        # Then 40-min blocks starting at 9:00 until around 17:00
        # Skip recess at 10:20–10:40 and 14:00–14:20 (approximate)
        hour = 9
        minute = 0
        end_hour = 17

        def time_to_str(h, m):
            return f"{h:02d}:{m:02d}"

        def add_40_minutes(h, m):
            m += 40
            if m >= 60:
                h += 1
                m -= 60
            return h, m

        while hour < end_hour:
            t_str = time_to_str(hour, minute)
            # Skip recess windows (approx 10:20–10:40 and 14:00–14:20)
            if not ((hour == 10 and minute == 20) or (hour == 14 and minute == 0)):
                slots.append(t_str)
            hour, minute = add_40_minutes(hour, minute)

    else:
        # Default: campus_a — 30-min slots 08:00–16:00, skipping 12:00–13:00 lunch
        for hour in range(8, 16):  # 8:00 to 15:30 last slot
            for minute in (0, 30):
                # Skip 12:00–13:00
                if hour == 12:
                    continue
                slots.append(f"{hour:02d}:{minute:02d}")

    return slots


# ---------- CORE SCHEDULER ----------

def run_scheduler(payload):
    """
    Core scheduling engine.

    Expects JSON-like dict payload:
    {
      "use_case": "learning_centre" | "clinic" | "general" | ...
      "schedule_template": "campus_a" | "campus_b",
      "clients": [
        {
          "name": "Nora",
          "sessions_needed": 3,
          "tag": "ELL",
          "spacing_rule": "once_per_day" | "none",
          "availability": {
            "Day1": ["08:00", "08:30", ...],
            "Day2": [...],
            ...
          }
        },
        ...
      ]
      // "groups": [...]  (reserved for future use)
    }

    Returns:
      timetable: { 'Day1': { '08:00': "Nora", ... }, ... }
      conflicts: [ "Unable to schedule Nora...", ... ]
      summary: {
         "Nora": { "needed": 3, "scheduled": 2 },
         ...
      }
      colors: { "Nora": "color-1", ... }  # for CSS class names
    """
    use_case = payload.get("use_case", "general")
    schedule_template = payload.get("schedule_template", "campus_a")
    clients = payload.get("clients", [])
    # groups = payload.get("groups", [])  # reserved for future use

    # Build base timetable structure: Day1–Day6, slots per template
    time_slots = build_time_slots(schedule_template)
    days = [f"Day{d}" for d in range(1, 7)]

    timetable = {
        day: {slot: "" for slot in time_slots}
        for day in days
    }

    # Assign a CSS color class to each client name for display
    color_classes = [
        "color-1", "color-2", "color-3", "color-4",
        "color-5", "color-6", "color-7", "color-8"
    ]
    student_colors = {}
    for idx, client in enumerate(clients):
        name = client.get("name", "").strip()
        if not name:
            continue
        student_colors[name] = color_classes[idx % len(color_classes)]

    conflicts = []
    summary = {}

    # Normalize clients and schedule them one by one
    for client in clients:
        name = client.get("name", "").strip()
        if not name:
            continue

        sessions_needed = int(client.get("sessions_needed", 0) or 0)
        spacing_rule = client.get("spacing_rule", "none")
        availability = client.get("availability", {})  # dict: DayX -> [times]

        # Normalize day keys to "Day1".."Day6"
        normalized_avail = {}
        for day_key, times in availability.items():
            if isinstance(day_key, str) and day_key.startswith("Day"):
                day = day_key
            else:
                # If it's just "1", "2", etc.
                day = f"Day{day_key}"
            normalized_avail.setdefault(day, [])
            normalized_avail[day].extend(times)

        scheduled_count = 0
        used_days_for_client = set()

        # Simple greedy scheduling:
        # iterate days in order, then times in order
        for day in days:
            # Enforce "once per day" rule if requested
            if spacing_rule == "once_per_day" and day in used_days_for_client:
                continue

            available_times = sorted(set(normalized_avail.get(day, [])))
            for slot in time_slots:
                if scheduled_count >= sessions_needed:
                    break

                if slot in available_times and timetable[day][slot] == "":
                    # Slot is free, and client is available here
                    timetable[day][slot] = name
                    scheduled_count += 1
                    used_days_for_client.add(day)

                    if spacing_rule == "once_per_day":
                        # Once one session is placed on this day, move to next day
                        break

            if scheduled_count >= sessions_needed:
                break

        if scheduled_count < sessions_needed:
            conflicts.append(
                f"Unable to fully schedule {name}: "
                f"needed {sessions_needed}, scheduled {scheduled_count}."
            )

        summary[name] = {
            "needed": sessions_needed,
            "scheduled": scheduled_count,
        }

    return timetable, conflicts, summary, student_colors


# ---------- ROUTES ----------

@app.route("/")
def index():
    # We don't yet have persistent data, so pass empty placeholders
    return render_template(
        "index.html",
        student_periods={},      # kept for forward compatibility if you reference it
        student_colors={},       # same idea – won't break Jinja
    )


@app.route("/generate_timetable", methods=["POST"])
def generate_timetable():
    """
    HTML flow:
    - Frontend sends JSON via fetch() from index.html
    - We run the scheduler
    - Return timetable.html with rendered results
    """
    data = request.get_json() or {}

    timetable, conflicts, summary, student_colors = run_scheduler(data)

    return render_template(
        "timetable.html",
        timetable=timetable,
        conflicts=conflicts,
        summary=summary,
        student_colors=student_colors,
        use_case=data.get("use_case", "general"),
        schedule_template=data.get("schedule_template", "campus_a"),
    )


@app.route("/api/generate_timetable", methods=["POST"])
def api_generate_timetable():
    """
    JSON API version:
      POST /api/generate_timetable
      Body: same payload expected by run_scheduler()
      Returns JSON: { timetable, conflicts, summary, student_colors }
    """
    data = request.get_json() or {}

    timetable, conflicts, summary, student_colors = run_scheduler(data)

    return jsonify(
        {
            "timetable": timetable,
            "conflicts": conflicts,
            "summary": summary,
            "student_colors": student_colors,
        }
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
