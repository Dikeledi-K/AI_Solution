"""ClinicQ – Streamlit entry point.

Run with:   streamlit run app.py

This file only lists the pages so the sidebar shows proper names and icons.
Each page's content lives in its own file.
"""

import streamlit as st

pages = [
    st.Page("home.py", title="Home", icon="🏠", default=True),
    st.Page("views/1_Patient_Intake.py", title="Patient Intake", icon="📝"),
    st.Page("views/2_Staff_Dashboard.py", title="Staff Dashboard", icon="🩺"),
    st.Page("views/4_Alerts.py", title="Alerts", icon="🚨"),
    st.Page("views/5_Patient_Records.py", title="Patient Records", icon="🗂️"),
    st.Page("views/3_Audit_Log.py", title="Audit Log", icon="📋"),
    st.Page("views/6_Staff.py", title="Staff", icon="👥"),
]
st.navigation(pages).run()
