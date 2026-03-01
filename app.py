import os
import re
from datetime import datetime, timezone

import requests
import pandas as pd
import streamlit as st
import plotly.express as px

# -----------------------------
# Config
# -----------------------------
FRESHDESK_DOMAIN = os.getenv("FRESHDESK_DOMAIN", "").strip()
FRESHDESK_API_KEY = os.getenv("FRESHDESK_API_KEY", "").strip()

BASE_URL = f"https://{FRESHDESK_DOMAIN}/api/v2"
AUTH = (FRESHDESK_API_KEY, "X")

STATUS = {
    2: "Open",
    3: "Pending",
    4: "Resolved",
    5: "Closed",
    6: "Waiting on Customer",
    7: "Waiting on Third Party",
}

SPR_SUBJECT_KEYWORD = "priority onboarding support"

# -----------------------------
# Helpers
# -----------------------------
def must_have_env():
    if not FRESHDESK_DOMAIN or not FRESHDESK_API_KEY:
        st.error("Missing env vars. Please export FRESHDESK_DOMAIN and FRESHDESK_API_KEY in the same terminal.")
        st.stop()

def iso_to_dt(s: str):
    if not s:
        return None
    # Freshdesk timestamps are usually Z
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)

def safe_get(d, k, default=None):
    return d.get(k, default) if isinstance(d, dict) else default

def fd_get(path, params=None):
    url = f"{BASE_URL}{path}"
    r = requests.get(url, params=params, auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()

def search_tickets_by_mid(mid: str):
    # This is the working query you found:
    # /search/tickets?query="custom_string:MID"
    query = f'"custom_string:{mid}"'
    data = fd_get("/search/tickets", params={"query": query})
    return data.get("results", []) or []

@st.cache_data(ttl=600)
def get_agent_name(agent_id):
    if not agent_id:
        return None
    try:
        a = fd_get(f"/agents/{agent_id}")
        return a.get("contact", {}).get("name") or a.get("name") or str(agent_id)
    except Exception:
        return str(agent_id)

def extract_closure_reason_from_ticket(ticket_full: dict):
    """
    Best-effort: try to pull a closure reason from common places.
    Freshdesk doesn't always have a clean 'closure reason' field exposed.
    We'll try:
      - custom_fields keys containing 'reason' / 'resolution'
      - 'description_text'
      - 'subject'
    """
    cf = ticket_full.get("custom_fields") or {}
    # 1) custom_fields candidates
    reason_keys = [k for k in cf.keys() if any(x in k.lower() for x in ["reason", "resolution", "closure", "close"])]
    for k in sorted(reason_keys):
        v = cf.get(k)
        if v:
            return f"{k}: {v}"

    # 2) description_text heuristic
    desc = (ticket_full.get("description_text") or ticket_full.get("description") or "").strip()
    if desc:
        # Try to find a "Reason:" style line
        m = re.search(r"(closure\s*reason|close\s*reason|reason|resolution)\s*[:\-]\s*(.+)", desc, re.IGNORECASE)
        if m:
            return m.group(0)[:2000]
    return ""

def get_ticket_full(ticket_id: int):
    return fd_get(f"/tickets/{ticket_id}")

# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title="Onboarding Ops Dashboard", layout="wide")
st.title("Onboarding Ops Dashboard")

must_have_env()

mid = st.text_input("Enter MID", value="", placeholder="e.g. RU3zC1UFgujVPc").strip()

if not mid:
    st.info("Enter a MID to fetch tickets.")
    st.stop()

# Fetch search results (light payload)
try:
    results = search_tickets_by_mid(mid)
except requests.HTTPError as e:
    st.error(f"Freshdesk search failed: {e}")
    st.stop()

# Filter SPR tickets by subject keyword
spr = [t for t in results if SPR_SUBJECT_KEYWORD in (t.get("subject") or "").lower()]

# Build dataframe (use updated_at as completion proxy)
rows = []
for t in spr:
    created = iso_to_dt(t.get("created_at"))
    updated = iso_to_dt(t.get("updated_at"))  # proxy for completion/last update
    tat_days = None
    if created and updated:
        tat_days = (updated - created).total_seconds() / 86400.0

    status_code = t.get("status")
    agent_id = t.get("responder_id") or t.get("agent_id")  # search payload may have responder_id
    rows.append(
        {
            "ticket_id": t.get("id"),
            "status": STATUS.get(status_code, f"Unknown({status_code})"),
            "created_at": created,
            "updated_at": updated,
            "tat_days": tat_days,
            "agent_id": agent_id,
            "subject": t.get("subject", ""),
        }
    )

df = pd.DataFrame(rows)

# Metrics
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total SPR Tickets", len(df))

if len(df) > 0 and df["tat_days"].notna().any():
    c2.metric("Avg TAT (Days)", round(df["tat_days"].mean(), 2))
    c3.metric("Max TAT (Days)", round(df["tat_days"].max(), 2))
    c4.metric("Min TAT (Days)", round(df["tat_days"].min(), 2))
else:
    c2.metric("Avg TAT (Days)", "-")
    c3.metric("Max TAT (Days)", "-")
    c4.metric("Min TAT (Days)", "-")

st.subheader("Status Breakdown")

if len(df) == 0:
    st.warning("No SPR tickets found for this MID.")
    st.stop()

# Bar chart
bar_df = df.groupby("status", as_index=False).size().rename(columns={"size": "count"})
fig_bar = px.bar(bar_df, x="status", y="count", title="Ticket Status Distribution (Bar)")
st.plotly_chart(fig_bar, use_container_width=True)

# Pie chart
fig_pie = px.pie(bar_df, names="status", values="count", title="Ticket Status Distribution (Pie)")
st.plotly_chart(fig_pie, use_container_width=True)

# TAT per ticket (timeline-ish)
df_plot = df.copy()
df_plot = df_plot.sort_values("created_at")
fig_tat = px.bar(
    df_plot,
    x="created_at",
    y="tat_days",
    color="status",
    title="TAT per Ticket (Days)",
    hover_data=["ticket_id", "subject"],
)
st.plotly_chart(fig_tat, use_container_width=True)

# Enrich with agent name + closure reason (fetch full ticket)
st.subheader("Ticket Details (Table)")

enriched_rows = []
for _, r in df.iterrows():
    ticket_id = int(r["ticket_id"])
    try:
        full = get_ticket_full(ticket_id)
        agent_id = full.get("responder_id") or r.get("agent_id")
        agent_name = get_agent_name(agent_id) if agent_id else ""
        closure_reason = extract_closure_reason_from_ticket(full)
    except Exception:
        agent_name = get_agent_name(r.get("agent_id"))
        closure_reason = ""

    enriched_rows.append(
        {
            "ticket_id": ticket_id,
            "status": r["status"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "tat_days": r["tat_days"],
            "agent": agent_name,
            "closure_reason": closure_reason,
            "subject": r["subject"],
        }
    )

edf = pd.DataFrame(enriched_rows)
st.dataframe(edf, use_container_width=True, hide_index=True)

st.subheader("Ticket Details (Expandable)")
for _, row in edf.iterrows():
    label = f"{row['ticket_id']} • {row['status']} • TAT {round(row['tat_days'], 4) if pd.notna(row['tat_days']) else '-'} days"
    with st.expander(label):
        st.write("Subject:", row["subject"])
        st.write("Created:", row["created_at"])
        st.write("Updated:", row["updated_at"])
        st.write("Agent:", row["agent"])
        st.write("Closure/Reason:", row["closure_reason"] or "(not found)")
