import json
from flask import Flask, render_template, request

app = Flask(__name__)

# ---------------------------------------------------------------------
# CAMPUS CONFIGS (edit timeslots here if needed)
# ---------------------------------------------------------------------
CAMPUS_CONFIGS = {
    "campus_30": {
        "label": "Campus A (30-minute periods, 12–1 lunch)",
        "time_slots": [
            "08:30–09:00",
            "09:00–09:30",
            "09:30–10:00",
            "10:00–10:30",
            "10:30–11:00",
            "11:00–11:30",
            "13:00–13:30",
            "13:30–14:00",
            "14:00–14:30",
            "14:30–15:00",
        ],
    },
    "campus_40": {
        "label": "Campus B (40-minute periods)",
        "time_slots": [
            "08:20–09:00",
            "09:00–09:40",
            "10:00–10:40",
            "10:40–11:20",
            "12:00–12:40",
            "12:40–13:20",
            "13:40–14:20",
            "14:20–15:00",
        ],
    },
}


# ---------------------------------------------------------------------
# HELPER: Find common availability for a group of students
# ---------------------------------------------------------------------
def find_common_availability(grouped_students):
    """
    grouped_students: list of dicts:
      {
        'name': str,
        'periods_needed': int,
        'availability': { '1': [...], '2': [...], ... }
      }
    Returns: dict like { '1': [...], '3': [...] } with overlapping time slots only.
    """
    if not grouped_students:
        return {}

    base = grouped_students[0]["availability"]
    common = {day: set(times) for day, times in base.items()}

    for stu in grouped_students[1:]:
        stu_av = stu["availability"]
        to_remove = []
        for day in list(common.keys()):
            if day in stu_av:
                common[day] &= set(stu_av[day])
                if not common[day]:
                    to_remove.append(day)
            else:
                to_remove.append(day)
        for d in to_remove:
            common.pop(d, None)

    return {d: sorted(list(v)) for d, v in common.items()}


# ---------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate_timetable", methods=["POST"])
def generate_timetable():
    data = request.get_json()
    if not data:
        return "No data received", 400

    campus_key = data.get("campus", "campus_30")
    campus = CAMPUS_CONFIGS.get(campus_key, CAMPUS_CONFIGS["campus_30"])
    time_slots = campus["time_slots"]
    campus_label = campus["label"]

    students_data = data.get("students", [])
    groups_data = data.get("groups", [])
    teacher_max = data.get("teacherMaxPerDay", {}) or {}
    teacher_unavail = data.get("teacherUnavailability", {}) or {}
    group_rules = data.get("groupRules", {}) or {}

    # Normalize students into a dict keyed by name
    name_to_student = {}
    for s in students_data:
        name = s.get("name")
        if not name:
            continue

        periods_needed = int(s.get("periodsNeeded", 0) or 0)
        availability = s.get("availability", {})

        normalized = {}
        for day, slots in availability.items():
            normalized[str(day)] = list(set(slots))

        name_to_student[name] = {
            "name": name,
            "periods_needed": periods_needed,
            "availability": normalized,
        }

    conflicts = []

    # Pre-check: students with 0 availability but >0 needed
    for name, stu in name_to_student.items():
        total_slots = sum(len(v) for v in stu["availability"].values())
        if stu["periods_needed"] > 0 and total_slots == 0:
            conflicts.append(f"{name} has no available times selected.")

    # Build groups list
    groups = []
    grouped_names = set()

    # Explicit groups from front-end
    for gnames in groups_data:
        group_students = [name_to_student[n] for n in gnames if n in name_to_student]
        if group_students:
            groups.append(group_students)
            for s in group_students:
                grouped_names.add(s["name"])

    # Any ungrouped student becomes their own group
    for name, stu in name_to_student.items():
        if name not in grouped_names:
            groups.append([stu])

    # Initialize empty timetable: Day1..Day6 × time_slots
    timetable = {
        f"Day{d}": {slot: "" for slot in time_slots}
        for d in range(1, 7)
    }

    # Track total teacher load per day (all groups combined)
    day_load = {str(d): 0 for d in range(1, 7)}

    # Color classes per group label
    color_classes = [
        "slot-color-1", "slot-color-2", "slot-color-3", "slot-color-4",
        "slot-color-5", "slot-color-6", "slot-color-7", "slot-color-8"
    ]
    group_colors = {}
    color_index = 0

    # Scheduling
    for group in groups:
        label = ", ".join([s["name"] for s in group])
        needed = max(s["periods_needed"] for s in group)
        if needed <= 0:
            continue

        # Assign a color
        if label not in group_colors:
            group_colors[label] = color_classes[color_index % len(color_classes)]
            color_index += 1

        common = find_common_availability(group)
        scheduled = 0

        for d in range(1, 7):
            day_key = str(d)
            tk = f"Day{d}"
            allowed = common.get(day_key, [])
            max_for_day = teacher_max.get(day_key)
            try:
                max_for_day = int(max_for_day) if max_for_day not in (None, "", 0) else None
            except (TypeError, ValueError):
                max_for_day = None

            for slot in time_slots:
                # skip if teacher unavailable
                if slot in teacher_unavail.get(day_key, []):
                    continue

                # respect teacher daily max
                if max_for_day is not None and day_load[day_key] >= max_for_day:
                    continue

                if (
                    scheduled < needed
                    and slot in allowed
                    and timetable[tk][slot] == ""
                ):
                    timetable[tk][slot] = label
                    scheduled += 1
                    day_load[day_key] += 1

            if scheduled >= needed:
                break

        if scheduled < needed:
            conflicts.append(
                f"Could not schedule all periods for [{label}] "
                f"(needed {needed}, scheduled {scheduled})."
            )

    days = [1, 2, 3, 4, 5, 6]

    # Compute scheduled counts per student
    scheduled_counts = {name: 0 for name in name_to_student.keys()}
    for d in days:
        dk = f"Day{d}"
        for slot in time_slots:
            label = timetable[dk][slot]
            if not label:
                continue
            names = [n.strip() for n in label.split(",")]
            for n in names:
                if n in scheduled_counts:
                    scheduled_counts[n] += 1

    student_periods = {name: stu["periods_needed"] for name, stu in name_to_student.items()}

    # JSON payload for timetable page (for copy / export / reload)
    timetable_json = {
        "campus_key": campus_key,
        "campus_label": campus_label,
        "time_slots": time_slots,
        "days": days,
        "timetable": timetable,
        "conflicts": conflicts,
        "students": students_data,
        "groups": groups_data,
        "teacherMaxPerDay": teacher_max,
        "teacherUnavailability": teacher_unavail,
        "groupRules": group_rules,
        "scheduledCounts": scheduled_counts,
        "studentPeriods": student_periods,
    }
    timetable_json_str = json.dumps(timetable_json)

    return render_template(
        "timetable.html",
        campus_label=campus_label,
        time_slots=time_slots,
        days=days,
        timetable=timetable,
        conflicts=conflicts,
        group_colors=group_colors,
        scheduled_counts=scheduled_counts,
        student_periods=student_periods,
        timetable_json_str=timetable_json_str,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
