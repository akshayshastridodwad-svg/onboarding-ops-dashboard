# mid_onboarding_streamlit.py
"""
Streamlit app: MID Onboarding Analyzer (Trino-backed)
- Paste a single MID and press Fetch
- App runs SQL against Trino, shows KPIs, timeline and charts, and a downloadable CSV
"""

import os
import re
import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
from datetime import datetime
import trino

st.set_page_config(page_title="MID Onboarding Analyzer", layout="wide")

# -------------------------
# Helper: Trino connection
# -------------------------
def get_trino_connection():
    host = os.environ.get("TRINO_HOST", "trino-host")
    port = int(os.environ.get("TRINO_PORT", 443))
    user = os.environ.get("TRINO_USER", None)
    password = os.environ.get("TRINO_PASSWORD", None)
    catalog = os.environ.get("TRINO_CATALOG", "hive")
    schema = os.environ.get("TRINO_SCHEMA", "default")
    http_scheme = os.environ.get("TRINO_HTTP_SCHEME", "https")

    # Basic auth if password provided, else anonymous
    auth = None
    if user and password:
        auth = trino.auth.BasicAuthentication(user, password)
        conn = trino.dbapi.connect(
            host=host,
            port=port,
            user=user,
            catalog=catalog,
            schema=schema,
            http_scheme=http_scheme,
            auth=auth,
        )
    elif user:
        conn = trino.dbapi.connect(
            host=host,
            port=port,
            user=user,
            catalog=catalog,
            schema=schema,
            http_scheme=http_scheme,
        )
    else:
        # anonymous
        conn = trino.dbapi.connect(
            host=host,
            port=port,
            catalog=catalog,
            schema=schema,
            http_scheme=http_scheme,
        )
    return conn

# -------------------------
# SQL: final query (no trailing semicolon)
# Replace {mid} with validated MID
# -------------------------
FINAL_SQL_TEMPLATE = r"""
WITH
target_merchant AS (
  SELECT trim('{mid}') AS merchant_id
),

-- 1) Ticket rows filtered to SRF/SPR Post-onboarding
tickets AS (
  SELECT
    t.ticket_id,
    t.merchant_id,
    COALESCE(t.subject, d.subject, '') AS subject,
    COALESCE(t.group_name, d.group_name, '') AS group_name,
    COALESCE(array_join(t.group_name_list, ','), d.group_name, '') AS group_name_list,
    t.created_timestamp,
    COALESCE(t.resolved_timestamp, d.resolved_at, d.closed_at) AS resolved_timestamp,
    t.current_status,
    t.last_status,
    COALESCE(NULLIF(t.yogi_resolution,''), NULLIF(t.resolution_status,''), NULLIF(d.cf_subcategory,''), NULLIF(d.cf_category,''), NULLIF(d.cf_item,'')) AS closure_reason,
    t.agent_name,
    t.agent_email,
    t.tags,
    t.added_tags,
    CASE WHEN COALESCE(t.resolved_timestamp, d.resolved_at, d.closed_at) IS NOT NULL
      THEN date_diff('second', t.created_timestamp, COALESCE(t.resolved_timestamp, d.resolved_at, d.closed_at))
      ELSE NULL END AS time_to_resolve_seconds,
    -- human readable resolution time
    CASE WHEN COALESCE(t.resolved_timestamp, d.resolved_at, d.closed_at) IS NOT NULL THEN
      concat(
        CAST(floor(date_diff('second', t.created_timestamp, COALESCE(t.resolved_timestamp, d.resolved_at, d.closed_at)) / 86400) AS varchar),
        ' days, ',
        CAST(floor((date_diff('second', t.created_timestamp, COALESCE(t.resolved_timestamp, d.resolved_at, d.closed_at)) % 86400) / 3600) AS varchar),
        ' hrs, ',
        CAST(floor((date_diff('second', t.created_timestamp, COALESCE(t.resolved_timestamp, d.resolved_at, d.closed_at)) % 3600) / 60) AS varchar),
        ' min'
      )
    ELSE NULL END AS time_to_resolve_readable,
    CASE
      WHEN lower(COALESCE(t.subject, d.subject, '')) LIKE '%srf%' THEN 'SRF'
      WHEN lower(COALESCE(t.subject, d.subject, '')) LIKE '%priority onboarding support%' THEN 'SPR'
      WHEN lower(COALESCE(t.group_name, d.group_name, '')) LIKE '%srf/spr post-onboarding%' OR lower(COALESCE(array_join(t.group_name_list, ','), '')) LIKE '%srf/spr post-onboarding%' OR lower(COALESCE(d.cf_ticket_queue, '')) LIKE '%post-onboarding%' THEN 'SRF/SPR'
      ELSE 'Other'
    END AS ticket_type
  FROM aggregate_ba.cs_freshdesk_tickets t
  LEFT JOIN aggregate_ba.freshdesk_data d ON t.ticket_id = d.id
  JOIN target_merchant tm ON t.merchant_id = tm.merchant_id
  WHERE (
    lower(COALESCE(t.group_name, d.group_name, '')) LIKE '%srf/spr post-onboarding%'
    OR lower(COALESCE(array_join(t.group_name_list, ','), '')) LIKE '%srf/spr post-onboarding%'
    OR lower(COALESCE(t.subject, d.subject, '')) LIKE '%priority onboarding support%'
    OR lower(COALESCE(d.cf_ticket_queue, '')) LIKE '%post-onboarding%'
    OR lower(COALESCE(d.cf_category, '')) LIKE '%post-onboarding%'
  )
),

-- 2) last agent aggregated note (ppg convs) (if available)
last_agent_notes AS (
  SELECT
    c.ticket_id,
    max(c.yr_mth) AS last_yr_mth,
    max(c.combined_body_text) AS last_agent_note
  FROM aggregate_ba.fd_conversations_sv_ppg_new c
  JOIN tickets t ON c.ticket_id = t.ticket_id
  GROUP BY c.ticket_id
),

-- 3) merchant activation from merchants table (handles ms/s)
merchant_act AS (
  SELECT
    m.id AS merchant_id,
    m.activated,
    m.activated_at,
    CASE
      WHEN m.activated_at IS NULL THEN NULL
      WHEN CAST(m.activated_at AS double) > 1e12 THEN from_unixtime(CAST(m.activated_at AS double) / 1000)
      ELSE from_unixtime(CAST(m.activated_at AS double))
    END AS activation_ts
  FROM realtime_hudi_api.merchants m
  JOIN target_merchant tm ON m.id = tm.merchant_id
  WHERE CAST(m.created_date AS date) BETWEEN DATE '2019-01-01' AND DATE '2026-12-31'
),

-- 4) activation candidate tickets (resolved rows with activation-like closure text or agent note)
activation_candidates AS (
  SELECT
    t.merchant_id,
    t.ticket_id,
    COALESCE(lan.last_agent_note, t.closure_reason, '') AS note_text,
    t.resolved_timestamp
  FROM tickets t
  LEFT JOIN last_agent_notes lan ON t.ticket_id = lan.ticket_id
  WHERE t.resolved_timestamp IS NOT NULL
    AND (
      lower(COALESCE(lan.last_agent_note, t.closure_reason, '')) LIKE '%activat%'
      OR lower(COALESCE(lan.last_agent_note, t.closure_reason, '')) LIKE '%go-live%'
      OR lower(COALESCE(lan.last_agent_note, t.closure_reason, '')) LIKE '%go live%'
      OR lower(COALESCE(lan.last_agent_note, t.closure_reason, '')) LIKE '%go-live%'
      OR lower(COALESCE(lan.last_agent_note, t.closure_reason, '')) LIKE '%live%'
    )
),

-- 5) choose earliest resolving activation-confirming ticket per merchant
activation_ticket AS (
  SELECT
    at.merchant_id,
    at.ticket_id AS activation_ticket_id,
    at.resolved_timestamp AS activation_ticket_ts
  FROM (
    SELECT ac.*,
      row_number() OVER (PARTITION BY ac.merchant_id ORDER BY ac.resolved_timestamp ASC) AS rn
    FROM activation_candidates ac
  ) at
  WHERE at.rn = 1
),

-- 6) merchant KPIs
merchant_summary AS (
  SELECT
    t.merchant_id,
    MIN(t.created_timestamp) AS first_ticket_created,
    MAX(CASE WHEN t.resolved_timestamp IS NOT NULL THEN t.resolved_timestamp ELSE NULL END) AS last_ticket_resolved,
    COUNT(*) AS total_tickets,
    SUM(CASE WHEN t.resolved_timestamp IS NOT NULL THEN 1 ELSE 0 END) AS resolved_tickets
  FROM tickets t
  GROUP BY t.merchant_id
),

-- 7) prefer activation ticket timestamp; fallback to merchant activation_ts
merchant_summary_with_activation AS (
  SELECT
    ms.*,
    COALESCE(at.activation_ticket_ts, ma.activation_ts) AS activation_timestamp_final,
    at.activation_ticket_id AS activation_confirming_ticket_id
  FROM merchant_summary ms
  LEFT JOIN merchant_act ma ON ms.merchant_id = ma.merchant_id
  LEFT JOIN activation_ticket at ON ms.merchant_id = at.merchant_id
),

-- 8) readable TAT
merchant_summary_final AS (
  SELECT
    ms.*,
    CASE
      WHEN ms.activation_timestamp_final IS NOT NULL AND ms.first_ticket_created IS NOT NULL
      THEN CAST(date_diff('second', ms.first_ticket_created, ms.activation_timestamp_final) AS BIGINT)
      ELSE NULL
    END AS tat_seconds_first_to_activation,
    CASE
      WHEN ms.activation_timestamp_final IS NOT NULL AND ms.first_ticket_created IS NOT NULL
      THEN concat(
        CAST(floor(date_diff('second', ms.first_ticket_created, ms.activation_timestamp_final) / 86400) AS varchar),
        ' days, ',
        CAST(floor((date_diff('second', ms.first_ticket_created, ms.activation_timestamp_final) % 86400) / 3600) AS varchar),
        ' hrs, ',
        CAST(floor((date_diff('second', ms.first_ticket_created, ms.activation_timestamp_final) % 3600) / 60) AS varchar),
        ' min'
      )
      ELSE NULL
    END AS tat_first_to_activation_readable
  FROM merchant_summary_with_activation ms
)

-- Final result (merchant_id, ticket_id first)
SELECT
  t.merchant_id,
  t.ticket_id,
  ms.first_ticket_created  AS first_ticket_created_ts,
  ms.last_ticket_resolved  AS last_ticket_resolved_ts,
  ms.activation_timestamp_final AS activation_timestamp_ts,
  ms.activation_confirming_ticket_id,
  ms.total_tickets,
  ms.resolved_tickets,
  ms.tat_seconds_first_to_activation,
  ms.tat_first_to_activation_readable,
  t.ticket_type,
  t.group_name,
  t.group_name_list,
  CAST(t.created_timestamp AS varchar) AS ticket_created_ts,
  CAST(t.resolved_timestamp AS varchar) AS ticket_resolved_ts,
  t.current_status,
  t.last_status,
  COALESCE(lan.last_agent_note, t.closure_reason) AS closure_reason_or_last_note,
  t.agent_name,
  t.agent_email,
  t.tags,
  t.added_tags,
  t.time_to_resolve_seconds,
  t.time_to_resolve_readable
FROM tickets t
LEFT JOIN last_agent_notes lan ON t.ticket_id = lan.ticket_id
LEFT JOIN merchant_summary_final ms ON t.merchant_id = ms.merchant_id
ORDER BY t.created_timestamp ASC
"""

# -------------------------
# Simple MID validator
# -------------------------
MID_RE = re.compile(r'^[A-Za-z0-9_\-]+$')

def validate_mid(mid: str) -> bool:
    return bool(MID_RE.match(mid.strip()))

# -------------------------
# Run SQL against Trino
# -------------------------
def run_trino_query(mid: str):
    conn = get_trino_connection()
    sql = FINAL_SQL_TEMPLATE.format(mid=mid)
    cur = conn.cursor()
    cur.execute(sql)
    cols = [c[0] for c in cur.description]
    rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    return df

# -------------------------
# UI
# -------------------------
st.title("MID Onboarding Analyzer — Freshdesk (Single MID)")
st.markdown("Paste a single MID, press **Fetch**, and the app will query Trino and render the dashboard. Make sure TRINO_* env vars are set on the server.")

with st.sidebar:
    st.header("Connection / Options")
    st.write("Trino host / creds read from environment. Set these env vars before running:")
    st.code("\n".join([
        "TRINO_HOST  (e.g. trino-querybook-coordinator.de.razorpay.com)",
        "TRINO_PORT  (e.g. 443)",
        "TRINO_USER",
        "TRINO_PASSWORD (optional)",
        "TRINO_CATALOG (default: hive)",
        "TRINO_SCHEMA (default: default)",
        "TRINO_HTTP_SCHEME (http or https)"
    ]), language="text")
    st.write("If you don't want to connect to Trino immediately, run locally and set env vars or use a CSV workflow (we can add that).")

# MID input
default_mid = os.environ.get("DEFAULT_MID", "")
mid_input = st.text_input("Enter MID (single)", value=default_mid, max_chars=64)
fetch_btn = st.button("Fetch")

# On fetch
if fetch_btn:
    mid = mid_input.strip()
    if mid == "":
        st.error("Please enter a MID.")
    elif not validate_mid(mid):
        st.error("MID contains unusual characters. Only letters, digits, underscore and hyphen allowed.")
    else:
        with st.spinner("Querying Trino for MID: " + mid):
            try:
                df = run_trino_query(mid)
            except Exception as e:
                st.exception(e)
                st.stop()

        if df.empty:
            st.warning("No rows found for this MID (check MID or filters).")
            st.stop()

        # Ensure merchant_id, ticket_id are first columns (they are in SQL, but enforce)
        cols = df.columns.tolist()
        # reorder if needed
        def ensure_front(cols, front):
            for c in reversed(front):
                if c in cols:
                    cols.remove(c)
                    cols.insert(0, c)
            return cols
        desired_front = ["merchant_id", "ticket_id"]
        cols = ensure_front(cols, desired_front)
        df = df[cols]

        # parse datetime columns and present human readable
        for c in ["first_ticket_created_ts", "last_ticket_resolved_ts", "activation_timestamp_ts", "ticket_created_ts", "ticket_resolved_ts"]:
            if c in df.columns:
                # try to coerce to datetime then format
                df[c] = pd.to_datetime(df[c], utc=True, errors="coerce").dt.tz_convert(None).dt.strftime("%Y-%m-%d %H:%M:%S")

        # Replace tat seconds column with readable if available
        if "tat_first_to_activation_readable" in df.columns:
            # keep both but hide seconds column if you like
            pass

        # KPI header
        st.header("Summary")
        # take first merchant_summary row
        ms = df.iloc[0]
        c1, c2, c3, c4 = st.columns([3,3,3,3])
        c1.metric("First ticket (created)", str(ms.get("first_ticket_created_ts", "N/A")))
        c2.metric("Activation timestamp", str(ms.get("activation_timestamp_ts", "Not found")))
        c3.metric("TAT (first → activation)", str(ms.get("tat_first_to_activation_readable", "Not available")))
        c4.metric("Total tickets", int(ms.get("total_tickets", 0)), delta=None)

        st.markdown("---")

        # Show table with merchant_id and ticket_id as first columns
        st.subheader("Tickets table")
        st.dataframe(df, height=420)

        # Download cleaned CSV
        csv = df.to_csv(index=False)
        st.download_button("Download CSV", csv, file_name=f"{mid}_freshdesk_report_clean.csv", mime="text/csv")

        # Timeline (Gantt)
        if "ticket_created_ts" in df.columns and "ticket_resolved_ts" in df.columns:
            df_plot = df.copy()
            df_plot["start"] = pd.to_datetime(df_plot["ticket_created_ts"], errors="coerce")
            df_plot["end"] = pd.to_datetime(df_plot["ticket_resolved_ts"], errors="coerce")
            df_plot["end"] = df_plot["end"].fillna(df_plot["start"] + pd.Timedelta(hours=1))
            df_plot["label"] = df_plot["ticket_id"].astype(str) + " — " + df_plot.get("ticket_type", "").astype(str)
            fig = px.timeline(df_plot, x_start="start", x_end="end", y="label",
                              color="ticket_type", hover_data=["closure_reason_or_last_note", "agent_email"])
            fig.update_yaxes(autorange="reversed")
            fig.update_layout(height=600, title="Ticket timeline (created → resolved)")
            st.plotly_chart(fig, use_container_width=True)

        # Resolution-time chart
        if "time_to_resolve_seconds" in df.columns and df["time_to_resolve_seconds"].notna().any():
            bar_df = df[df["time_to_resolve_seconds"].notna()].copy()
            bar_df["hours"] = bar_df["time_to_resolve_seconds"] / 3600.0
            fig2 = px.bar(bar_df.sort_values("ticket_created_ts"), x="ticket_id", y="hours", color="ticket_type",
                          hover_data=["closure_reason_or_last_note", "agent_email"])
            fig2.update_layout(height=400, title="Time to resolve (hours)")
            st.plotly_chart(fig2, use_container_width=True)

        st.success("Done — results fetched from Trino.")