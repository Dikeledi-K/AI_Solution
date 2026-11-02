"""Waiting-room screen: a TV in the waiting area shows who is being called and where.

For patients without a phone (or airtime), this replaces the SMS. Privacy:
    - only QUEUE NUMBERS and the neutral DOOR number ("Room 12") are shown;
    - never a name, a triage colour, or what the room is for (e.g. HIV testing).
No sign-in is needed, so the screen can stay open all day. It refreshes on its own.
Open it full screen on the TV (F11 in most browsers).
"""

from datetime import datetime

import streamlit as st

from core import database as db
from core import rooms
from core.ui import page_setup

REFRESH_SECONDS = 10

page_setup("Waiting Room", "📺")
db.init_db()

st.markdown("""
<style>
.cq-board { background: linear-gradient(135deg,#0f766e,#1e3a8a); border-radius: 24px; padding: 28px 32px;
  color: #fff; box-shadow: 0 18px 40px rgba(15,118,110,.25); }
.cq-board h1 { color: #fff; font-size: 2.6rem; margin: 0 0 4px 0; }
.cq-board .sub { color: #ccfbf1; font-size: 1.15rem; margin-bottom: 18px; }
.cq-calls { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }
.cq-call { background: rgba(255,255,255,.12); border-radius: 18px; padding: 18px 22px; display: flex;
  align-items: center; justify-content: space-between; }
.cq-call.latest { background: #fef3c7; color: #78350f; animation: cq-pulse 1.6s ease-in-out 3; }
.cq-call .num { font-size: 3.4rem; font-weight: 800; line-height: 1; }
.cq-call .room { font-size: 1.6rem; font-weight: 700; text-align: right; }
.cq-call .room small { display: block; font-size: .95rem; font-weight: 500; opacity: .85; }
.cq-board .empty { font-size: 1.4rem; color: #ccfbf1; padding: 14px 0; }
.cq-foot { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-top: 18px;
  font-size: 1.1rem; color: #e0f2fe; }
@keyframes cq-pulse { 0%,100% { transform: scale(1); } 50% { transform: scale(1.04); } }
</style>
""", unsafe_allow_html=True)


@st.fragment(run_every=REFRESH_SECONDS)
def board() -> None:
    called = sorted(db.list_patients(("called",)), key=lambda p: p.get("called_time") or "", reverse=True)
    waiting = len(db.list_patients(("waiting",)))
    if called:
        cards = "".join(
            f"<div class='cq-call{' latest' if i == 0 else ''}'><div class='num'>#{p['queue_number']}</div>"
            f"<div class='room'>➜ {rooms.door_name(p.get('room'))}<small>"
            f"called {(p.get('called_time') or '')[11:16]}</small></div></div>"
            for i, p in enumerate(called)
        )
    else:
        cards = "<div class='empty'>Nobody is being called right now. Please stay in the waiting area.</div>"
    st.markdown(
        "<div class='cq-board'><h1>📣 Now calling</h1>"
        "<div class='sub'>When you see your queue number, please go to that room.</div>"
        f"<div class='cq-calls'>{cards}</div>"
        f"<div class='cq-foot'><span>👥 {waiting} waiting</span>"
        "<span>⚠️ Feeling worse while you wait? Tell a nurse straight away.</span>"
        f"<span>🕘 {datetime.now():%H:%M}</span></div></div>",
        unsafe_allow_html=True,
    )


board()
st.caption("This screen updates itself every few seconds. It only shows queue numbers and room numbers – "
           "never names or health information.")
