import os
import time
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
import streamlit as st
import streamlit.components.v1 as components
from fpdf import FPDF # ✅ NEW: For clean PDF generation

# =========================
# CONFIG & DICTIONARIES
# =========================
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "").strip().rstrip("/")
N8N_WEBHOOK_PATH = os.getenv("N8N_WEBHOOK_PATH", "mid_intelligence").strip()
SHOW_ADVANCED = os.getenv("SHOW_ADVANCED", "0") == "1"
IST = timezone(timedelta(hours=5, minutes=30))

CAFFEINE_STATUS_LINES = ["Brewing context — tickets, blockers, and next steps…", "Warming up the dashboard — one sip at a time… ☕"]

FRESHDESK_GROUPS = {
    82000413959: "Risk",
    82000123456: "Checkers" 
}

TOOLTIPS = {
    "Main tickets": "Total parent tickets explicitly associated with this MID.",
    "Child tickets": "Total internal tasks or sub-tickets linked to the parent tickets.",
    "Total Interactions": "Total number of messages, replies, and notes across all tickets.",
    "Avg TAT (days)": "Average Turn Around Time (creation to last update) across all tickets.",
    "Max TAT (days)": "The single longest time a ticket has remained open/unresolved.",
    "Min TAT (days)": "The fastest resolution time for a single ticket.",
    "Aging (days)": "Total days between the oldest ticket's creation and the newest ticket's last update."
}

def get_group_name(gid):
    if not gid or str(gid).lower() == "unassigned": return "Unassigned"
    try: return FRESHDESK_GROUPS.get(int(gid), f"ID: {gid}")
    except: return str(gid)

def get_status_emoji(status: str) -> str:
    s = str(status).lower()
    if "closed" in s or "resolved" in s or "activated" in s: return f"🟢 {status}"
    if "pending" in s or "waiting" in s or "clarification" in s: return f"🟡 {status}"
    if "open" in s: return f"🔴 {status}"
    return f"🔵 {status}"

# =========================
# HELPERS
# =========================
def parse_iso(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str: return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception: return None

def fmt_ist(dt: Optional[datetime]) -> str:
    if not dt: return "—"
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p")

def get_time_ago(dt: Optional[datetime]) -> str:
    if not dt: return ""
    now = datetime.now(timezone.utc)
    diff = now - dt
    days = diff.days
    if days < 0: return "Just now"
    if days == 0: return "Today"
    if days == 1: return "Yesterday"
    return f"{days} days ago"

def _clean_text(s: str, max_len: int = 320) -> str:
    s = (s or "").strip()
    if not s: return ""
    s = " ".join(s.split())
    return (s[: max_len - 1] + "…") if len(s) > max_len else s

def extract_closure_reason(ticket: Dict[str, Any]) -> str:
    for key in ("closure_reason", "last_reply_text", "last_public_reply", "last_agent_reply", "last_note"):
        val = ticket.get(key)
        if isinstance(val, str) and val.strip(): return _clean_text(val)
    return "—"

def post_to_n8n(mid: str) -> Dict[str, Any]:
    resp = requests.post(f"{N8N_WEBHOOK_URL}/{N8N_WEBHOOK_PATH}", json={"mid": mid}, timeout=60)
    resp.raise_for_status()
    return resp.json()

# ✅ BACKEND PDF GENERATOR (Clean, no UI artifacts)
def create_pdf_bytes(payload, ai_summary):
    class PDF(FPDF):
        def header(self):
            self.set_font('Arial', 'B', 15)
            self.cell(0, 10, 'Merchant Intelligence Summary', 0, 1, 'C')
            self.set_font('Arial', 'I', 10)
            self.cell(0, 6, f"Generated on {datetime.now(IST).strftime('%d %b %Y, %I:%M %p')}", 0, 1, 'C')
            self.line(10, 28, 200, 28)
            self.ln(10)

    def clean_txt(t):
        # Strips emojis and special chars to prevent FPDF unicode errors
        return str(t).encode('latin-1', 'ignore').decode('latin-1')

    pdf = PDF()
    pdf.add_page()
    
    # Header Info
    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 8, clean_txt(f"Merchant: {payload.get('business_name', 'N/A')}"), 0, 1)
    pdf.set_font('Arial', '', 11)
    pdf.cell(0, 6, clean_txt(f"MID: {payload.get('mid', 'N/A')}"), 0, 1)
    pdf.cell(0, 6, clean_txt(f"Status: {payload.get('mid_status', 'N/A')}"), 0, 1)
    pdf.cell(0, 6, clean_txt(f"Category/MCC: {payload.get('category', '—')} / {payload.get('mcc_code', '—')}"), 0, 1)
    pdf.ln(6)

    # Next Step
    pdf.set_font('Arial', 'B', 12)
    pdf.set_fill_color(245, 245, 245)
    pdf.cell(0, 8, " Recommended Action", 0, 1, fill=True)
    pdf.set_font('Arial', 'B', 11)
    pdf.multi_cell(0, 6, clean_txt(ai_summary.get("next_step", "None")))
    pdf.ln(6)

    # Summary
    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 8, " Case History", 0, 1, fill=True)
    pdf.set_font('Arial', '', 11)
    pdf.multi_cell(0, 6, clean_txt(ai_summary.get("summary", "None")))
    pdf.ln(4)

    # Bullets
    for kp in ai_summary.get("key_points", []):
        pdf.set_font('Arial', '', 10)
        pdf.multi_cell(0, 6, clean_txt(f"- {kp}"))
        pdf.ln(2)
        
    # Metrics
    pdf.ln(4)
    pdf.set_font('Arial', 'B', 12)
    pdf.cell(0, 8, " Operational Metrics", 0, 1, fill=True)
    pdf.set_font('Arial', '', 11)
    pdf.cell(0, 6, clean_txt(f"Total Interactions: {payload.get('total_interactions', 0)}"), 0, 1)
    pdf.cell(0, 6, clean_txt(f"Avg TAT: {payload.get('avg_tat_days', '—')} days"), 0, 1)
    pdf.cell(0, 6, clean_txt(f"Aging: {payload.get('aging_days', '—')} days"), 0, 1)

    out = pdf.output(dest='S')
    # Handle FPDF version string/byte formatting differences
    if isinstance(out, str): return out.encode('latin-1')
    return bytes(out)

# =========================
# PAGE + PREMIUM THEME
# =========================
st.set_page_config(layout="wide", page_title="Onboarding Ops Dashboard - V2")

st.markdown(
    """
    <style>
      .stApp {
        background: radial-gradient(1200px 600px at 15% 10%, rgba(99,102,241,0.20), transparent 60%),
                    radial-gradient(1000px 600px at 85% 20%, rgba(16,185,129,0.16), transparent 55%),
                    radial-gradient(800px 500px at 60% 85%, rgba(236,72,153,0.12), transparent 55%),
                    linear-gradient(180deg, #070A12 0%, #060913 35%, #070A12 100%);
        color: rgba(255,255,255,0.92);
      }
      .block-container { padding-top: 2.2rem; padding-bottom: 2.2rem; max-width: 1200px; }
      header, #MainMenu, footer { visibility: hidden; }
      .title { font-size: 44px; font-weight: 900; line-height: 1.05; margin: 0 0 6px 0; letter-spacing: -0.02em;}
      .subtitle { color: rgba(255,255,255,0.64); font-size: 15px; margin-bottom: 22px; }
      div[data-baseweb="input"] input {
        background: rgba(255,255,255,0.06) !important;
        border: 1px solid rgba(255,255,255,0.12) !important;
        border-radius: 14px !important;
        padding: 12px 12px !important;
        color: rgba(255,255,255,0.92) !important;
      }
      .stButton button {
        border-radius: 14px;
        border: 1px solid rgba(255,255,255,0.16);
        background: linear-gradient(90deg, rgba(99,102,241,0.88), rgba(16,185,129,0.80));
        color: white; font-weight: 900; padding: 10px 14px; box-shadow: 0 10px 26px rgba(0,0,0,0.35);
      }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="title">Onboarding Ops Dashboard - V2</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">Fast context for Ops/Agents — tickets, blockers, resolution & metrics.</div>', unsafe_allow_html=True)

if "payload" not in st.session_state:
    st.session_state.payload = None

c1, c2 = st.columns([5, 1])
with c1: mid = st.text_input("Enter MID", label_visibility="collapsed", placeholder="Enter MID", key="mid_input")
with c2: fetch = st.button("Fetch", use_container_width=True)

def status_pill_html(status_text: str) -> str:
    s_lower = (status_text or "").strip().lower().replace("_", " ")
    if "activated" in s_lower and "not" not in s_lower: dot = "rgba(16,185,129,0.95)"
    elif "clarification" in s_lower: dot = "rgba(245,158,11,0.95)"
    elif "review" in s_lower: dot = "rgba(59,130,246,0.95)"
    elif any(x in s_lower for x in ["not activated", "rejected", "deactivated", "none"]): dot = "rgba(239,68,68,0.95)"
    else: dot = "rgba(99,102,241,0.95)"
    return f"""<span style="display:inline-flex; align-items:center; gap:8px; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:900; border:1px solid rgba(255,255,255,0.12); background: rgba(255,255,255,0.06);"><span style="width:8px;height:8px;border-radius:50%;background:{dot};display:inline-block;"></span>{(status_text or "—").replace("_", " ").title()}</span>"""

def glass_wrap(inner: str) -> str:
    return f"""<div style="background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.10); border-radius: 18px; padding: 18px; box-shadow: 0 12px 35px rgba(0,0,0,0.35); backdrop-filter: blur(10px);">{inner}</div>"""

def render_html_card(inner_html: str, height: int):
    html = f"""<html><head><meta charset="utf-8" /><style>body {{ margin:0; font-family: ui-sans-serif, system-ui; color: rgba(255,255,255,0.92); background: transparent; overflow-y: auto; }} ::-webkit-scrollbar {{ width: 6px; }} ::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.15); border-radius: 10px; }} .muted {{ color: rgba(255,255,255,0.62); }} hr {{ border:none; border-top:1px solid rgba(255,255,255,0.10); margin:16px 0; }} .section-title {{ font-size: 18px; font-weight: 950; margin-bottom: 10px; }} .grid2 {{ display:grid; grid-template-columns: 1fr 1.2fr; gap: 24px; }} .kitem {{ margin:6px 0; line-height: 1.4; }}</style></head><body>{inner_html}</body></html>"""
    components.html(html, height=height, scrolling=True)

if fetch:
    mid = (mid or "").strip()
    if mid:
        with st.status("Warming up the ops console… ☕", expanded=False) as s:
            s.update(label=random.choice(CAFFEINE_STATUS_LINES), state="running")
            try: 
                st.session_state.payload = post_to_n8n(mid)
            except Exception as e:
                st.error(f"Error fetching intelligence: {e}")
                st.session_state.payload = None
            s.update(label="Served hot. ✅", state="complete")

if st.session_state.payload:
    payload = st.session_state.payload
    
    status = payload.get("mid_status", "—")
    activation = payload.get("mid_activation_date")
    progress = payload.get("mid_progress")
    tickets = payload.get("tickets") or []
    ai_summary = payload.get("ai_summary") or {}
    status_lower = str(status).lower().replace("_", " ")

    # Generate Text Report (For direct copy-pasting)
    report_text = f"""==================================================
MERCHANT INTELLIGENCE SUMMARY
==================================================
MID: {payload.get("mid", "N/A")}
Business Name: {payload.get("business_name", "N/A")}
Status: {status}

--------------------------------------------------
RECOMMENDED ACTION:
{ai_summary.get("next_step", "None")}

--------------------------------------------------
CASE HISTORY:
{ai_summary.get("summary", "None")}

KEY POINTS:
"""
    for kp in ai_summary.get("key_points", []):
        report_text += f" - {kp}\n"
        
    report_text += f"""
--------------------------------------------------
METRICS:
Total Interactions: {payload.get("total_interactions", 0)}
Avg TAT: {payload.get("avg_tat_days", "N/A")} days
Max TAT: {payload.get("max_tat_days", "N/A")} days
Aging: {payload.get("aging_days", "N/A")} days
==================================================
Generated on: {datetime.now(IST).strftime('%d %b %Y, %I:%M %p IST')}
"""
    # Generate pure PDF report
    pdf_bytes = create_pdf_bytes(payload, ai_summary)

    # ✅ ACTION BAR (Export Buttons)
    st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)
    btn_col1, btn_col2, btn_col3 = st.columns([5, 1.5, 1.5])
    with btn_col2:
        st.download_button("📄 Download Brief (.txt)", data=report_text, file_name=f"{payload.get('mid')}_Case_Brief.txt", use_container_width=True)
    with btn_col3:
        st.download_button("🖨️ Download PDF", data=pdf_bytes, file_name=f"{payload.get('mid')}_Case_Brief.pdf", mime="application/pdf", use_container_width=True)

    if "clarification" in status_lower: prog_color = "rgba(245,158,11,0.92)"
    elif "review" in status_lower: prog_color = "rgba(59,130,246,0.92)"
    elif "activated" in status_lower and "not" not in status_lower: prog_color = "rgba(16,185,129,0.92)"
    else: prog_color = "rgba(239,68,68,0.92)"

    try:
        prog_bar_width = min(max(float(str(progress).replace('%', '')), 0), 100)
    except:
        prog_bar_width = 0

    prog_val = f"{progress}%" if str(progress).replace('.', '', 1).isdigit() else str(progress or "Pending")
    
    if status_lower == "activated" and activation and activation not in ["—", "None"]:
        sub_html = f"""<div class="muted" style="margin-top:8px;">Activation: <span style="font-weight:900; color:rgba(255,255,255,0.92)">{activation}</span></div>"""
    else:
        sub_html = f"""
            <div class="muted" style="margin-top:8px;">Form Progress: <span style="font-weight:900; color:{prog_color}">{prog_val}</span></div>
            <div style="width: 100%; background-color: rgba(255,255,255,0.1); border-radius: 4px; margin-top: 6px; height: 5px;">
              <div style="width: {prog_bar_width}%; background-color: {prog_color}; height: 5px; border-radius: 4px; box-shadow: 0 0 8px {prog_color}; transition: width 1s ease-in-out;"></div>
            </div>
        """

    render_html_card(glass_wrap(f"""<div style="display:flex; justify-content:space-between; align-items:flex-start; gap:16px;"><div><div style="font-size:22px; font-weight:950; margin-bottom:6px;">{payload.get("business_name", "Merchant")}</div><div class="muted">MID: <span style="font-weight:900; color:rgba(255,255,255,0.92)">{payload.get("mid")}</span></div></div><div style="text-align:right; width: 220px;">{status_pill_html(status)}{sub_html}<div class="muted" style="margin-top: 8px;">Category: <span style="font-weight:900; color:rgba(255,255,255,0.92)">{payload.get("category", "—")} / {payload.get("mcc_code", "—")}</span></div></div></div>"""), height=160)
    st.markdown("")

    next_step_text = ai_summary.get("next_step", "—")
    if "no immediate action" in next_step_text.lower():
        ns_bg, ns_border, ns_text = "rgba(16,185,129,0.1)", "rgba(16,185,129,0.2)", "#34d399"
    else:
        ns_bg, ns_border, ns_text = "rgba(245,158,11,0.15)", "rgba(245,158,11,0.3)", "#fbbf24"

    kp_html = ""
    for k in ai_summary.get("key_points", []):
        kp_html += f"""
        <div style="position: relative; padding-left: 20px; margin-bottom: 16px; border-left: 2px solid rgba(255,255,255,0.15);">
            <div style="position: absolute; left: -5px; top: 6px; width: 8px; height: 8px; border-radius: 50%; background: rgba(255,255,255,0.5); box-shadow: 0 0 5px rgba(255,255,255,0.3);"></div>
            <div style="font-size: 14px; line-height: 1.5; color: rgba(255,255,255,0.85);">{k}</div>
        </div>
        """
    if not kp_html:
        kp_html = "—"

    render_html_card(glass_wrap(f"""
        <div class="section-title">Merchant Intelligence Summary</div>
        <div style="font-size:15px; line-height:1.6;">{ai_summary.get("summary", "—")}</div>
        <hr/>
        <div class="grid2">
          <div>
            <div style="font-weight:950; margin-bottom:8px; color:rgba(255,255,255,0.7);">Recommended Action</div>
            <div style="font-size:14.5px; line-height:1.5; color:{ns_text}; font-weight:700; background:{ns_bg}; padding:12px 14px; border-radius:8px; border:1px solid {ns_border}; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
              {next_step_text}
            </div>
          </div>
          <div>
            <div style="font-weight:950; margin-bottom:12px; color:rgba(255,255,255,0.7);">Case history</div>
            <div>{kp_html}</div>
          </div>
        </div>
    """), height=300)
    st.markdown("")

    main_tickets = [t for t in tickets if not t.get("is_child", False)]
    child_tickets = [t for t in tickets if t.get("is_child", False)]

    # ✅ METRICS TILE UPDATED TO 'Total Interactions'
    m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
    metrics_data = [
        (m1, "Main tickets", len(main_tickets), len(main_tickets)),
        (m2, "Child tickets", len(child_tickets), len(child_tickets)),
        (m3, "Total Interactions", payload.get("total_interactions", 0), payload.get("total_interactions", 0)),
        (m4, "Avg TAT (days)", payload.get("avg_tat_days", "—"), payload.get("avg_tat_days")),
        (m5, "Max TAT (days)", payload.get("max_tat_days", "—"), payload.get("max_tat_days")),
        (m6, "Min TAT (days)", payload.get("min_tat_days", "—"), payload.get("min_tat_days")),
        (m7, "Aging (days)", payload.get("aging_days", "—"), payload.get("aging_days")),
    ]

    for col, label, display_val, raw_val in metrics_data:
        border, bg, txt, icon = "rgba(255,255,255,0.10)", "rgba(255,255,255,0.05)", "white", ""
        if label in ["Avg TAT (days)", "Aging (days)"] and isinstance(raw_val, (int, float)):
            if raw_val > 3:
                border, bg, txt, icon = "rgba(239,68,68,0.5)", "rgba(239,68,68,0.15)", "#ff6b6b", " 🔴"
            elif raw_val < 1:
                border, bg, txt, icon = "rgba(16,185,129,0.5)", "rgba(16,185,129,0.15)", "#4ade80", " 🟢"

        with col:
            st.markdown(f"""
            <div title="{TOOLTIPS.get(label, '')}" style="background: {bg}; border: 1px solid {border}; border-radius: 16px; padding: 14px; cursor: help;">
                <div style="color: rgba(255,255,255,0.62); font-size: 12px; font-weight: 800;">{label}{icon}</div>
                <div style="font-size: 28px; font-weight: 950; margin-top: 6px; color: {txt};">{display_val}</div>
            </div>
            """, unsafe_allow_html=True)
            
    st.markdown("")
    
    ticket_col_config = {
        "Ticket ID": st.column_config.LinkColumn("Ticket ID", help="Click to open ticket in Freshdesk", display_text=r"https://razorpay-ind\.freshdesk\.com/a/tickets/(\d+)"),
        "Child Ticket": st.column_config.LinkColumn("Child Ticket", help="Click to open child ticket", display_text=r"https://razorpay-ind\.freshdesk\.com/a/tickets/(\d+)"),
        "Parent Ticket": st.column_config.LinkColumn("Parent Ticket", help="Click to open parent ticket", display_text=r"https://razorpay-ind\.freshdesk\.com/a/tickets/(\d+)")
    }

    if main_tickets:
        st.markdown("""<div style="background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.10); border-radius: 18px; padding: 18px; box-shadow: 0 12px 35px rgba(0,0,0,0.35); backdrop-filter: blur(10px);"><div style="font-size:18px; font-weight:950; margin-bottom:10px;">Ticket Details</div></div>""", unsafe_allow_html=True)
        main_rows = [{
            "Ticket ID": f"https://razorpay-ind.freshdesk.com/a/tickets/{t.get('ticket_id')}",
            "Status": get_status_emoji(t.get("status")),
            "Created": fmt_ist(parse_iso(t.get("created_at"))),
            "Updated": f"{fmt_ist(parse_iso(t.get('updated_at')))}  ({get_time_ago(parse_iso(t.get('updated_at')))})",
            "Interactions": t.get("interaction_count", 0), 
            "TAT (days)": t.get("tat_days", "—"),
            "Child Tickets": t.get("linked_children", "None"),
            "Subject": t.get("subject"),
            "Closure Reason": extract_closure_reason(t),
        } for t in main_tickets]
        st.dataframe(main_rows, use_container_width=True, hide_index=True, column_config=ticket_col_config)
    else:
        st.markdown(glass_wrap("""
        <div style="text-align: center; padding: 30px 10px;">
            <div style="font-size: 32px; margin-bottom: 12px; opacity: 0.7;">📭</div>
            <div style="font-size: 18px; font-weight: 900; margin-bottom: 6px;">No Main Tickets Found</div>
            <div style="color: rgba(255,255,255,0.5); font-size: 14px;">This merchant has no active or historical parent tickets in the system.</div>
        </div>
        """), unsafe_allow_html=True)

    if child_tickets:
        child_rows = [{
            "Child Ticket": f"https://razorpay-ind.freshdesk.com/a/tickets/{t.get('ticket_id')}",
            "Parent Ticket": f"https://razorpay-ind.freshdesk.com/a/tickets/{t.get('parent_ticket_id')}" if str(t.get("parent_ticket_id")).isdigit() else "None",
            "Status": get_status_emoji(t.get("status")),
            "Created": fmt_ist(parse_iso(t.get("created_at"))),
            "Updated": f"{fmt_ist(parse_iso(t.get('updated_at')))}  ({get_time_ago(parse_iso(t.get('updated_at')))})",
            "Interactions": t.get("interaction_count", 0), 
            "TAT (days)": t.get("tat_days", "—"),
            "Assigned Group": get_group_name(t.get("group_id")),
            "Closure Reason": extract_closure_reason(t),
        } for t in child_tickets]
        
        with st.expander(f"📂 View Child Tickets ({len(child_rows)})"):
            st.dataframe(child_rows, use_container_width=True, hide_index=True, column_config=ticket_col_config)

    if SHOW_ADVANCED:
        with st.expander("Debug payload"): st.json(payload)
