"""Daily report for the facility manager (and admins): numbers only, no patient names."""

import csv
import io
from datetime import date

import pandas as pd
import streamlit as st

from core.auth import staff_label
from core.auth_ui import require_staff
from core import database as db
from core.reports import COLOURS, daily_report
from core.ui import URGENCY_STYLE, hero, page_setup, section, tile, tiles

page_setup("Reports", "📊")
hero("Daily report", "Waiting times, outcomes and how the AI is doing – numbers only, no names", "📊", "audit")
account = require_staff("reports")

day = st.date_input("Day", value=date.today(), max_value=date.today(), format="DD/MM/YYYY")
r = daily_report(day)

tiles([
    tile("Registered", r["registered"], "📝", "blue"),
    tile("Finished", r["finished"], "✔️", "GREEN"),
    tile("Still in clinic", r["still_in_clinic"], "⏳", "slate"),
    tile("Referred", r["outcomes"]["REFERRED"], "🚑", "ORANGE"),
    tile("Overdue alerts", r["overdue"], "⏰", "pink"),
    tile("AI failed (manual review)", r["ai_failed"], "🤖", "violet"),
])

# --- Waiting times --------------------------------------------------------------
section("⏱️ Waiting time until first called to Vitals", "South African Triage Scale targets")
rows = []
for c in COLOURS:
    w = r["wait_to_vitals"][c]
    target = "immediately" if w["target_minutes"] == 0 else f"≤ {w['target_minutes']} min"
    rows.append({"Colour": c, "Patients": r["by_colour"][c], "Called so far": w["called"],
                 "Median wait (min)": w["median_minutes"] if w["median_minutes"] is not None else "—",
                 "Target": target,
                 "Seen within target": f"{w['within_target_pct']}%" if w["within_target_pct"] is not None else "—"})
wait_table = pd.DataFrame(rows)


def colour_cell(value):
    style = URGENCY_STYLE.get(value)
    return f"background-color:{style['bg']};color:{style['fg']};font-weight:700" if style else ""


st.dataframe(wait_table.style.map(colour_cell, subset=["Colour"]), width="stretch", hide_index=True)
if r["median_minutes_in_clinic"] is not None:
    st.caption(f"Median time in the clinic for finished visits: **{r['median_minutes_in_clinic']} min** "
               "(arrival to going home or referral).")

# --- AI monitoring --------------------------------------------------------------
section("🗣️ Nurse changes to the suggested colour, by language",
        "watch for language bias – see RESPONSIBLE_AI.md")
lang_rows = [{"Language": lang, "Patients": v["patients"], "Colour confirmed by nurse": v["reviewed"],
              "Nurse RAISED the colour": v["raised"], "Nurse lowered the colour": v["lowered"],
              "Raised %": f"{round(100 * v['raised'] / v['reviewed'])}%" if v["reviewed"] else "—"}
             for lang, v in r["languages"].items()]
if lang_rows:
    st.dataframe(pd.DataFrame(lang_rows), width="stretch", hide_index=True)
    st.caption("“Raised” means the vitals nurse found the patient MORE urgent than rules + AI suggested. "
               "If one language is raised much more often than English over several days, the AI may be "
               "under-rating that language: review its danger-sign phrases with nurses who speak it.")
else:
    st.info("No patients registered on this day.")

# --- Export ------------------------------------------------------------------------
buffer = io.StringIO()
writer = csv.writer(buffer)
writer.writerow(["section", "item", "value"])
for key in ("registered", "finished", "still_in_clinic", "overdue", "ai_failed", "median_minutes_in_clinic"):
    writer.writerow(["summary", key, r[key]])
for outcome, n in r["outcomes"].items():
    writer.writerow(["outcome", outcome, n])
for row in rows:
    for key, value in row.items():
        if key != "Colour":
            writer.writerow([f"wait {row['Colour']}", key, value])
for row in lang_rows:
    for key, value in row.items():
        if key != "Language":
            writer.writerow([f"language {row['Language']}", key, value])


def log_export() -> None:
    db.add_audit("EXPORT", nurse_action=f"Daily report {r['day']} exported by {staff_label(account)}")


st.download_button("⬇️ Export report as CSV", data=buffer.getvalue(), file_name=f"clinicq_report_{r['day']}.csv",
                   mime="text/csv", on_click=log_export)
