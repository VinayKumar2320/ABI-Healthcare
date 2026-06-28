import os
import re
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta

# Set page configuration
st.set_page_config(
    page_title="Medicare Part B Wound Care Analytics",
    page_icon="🩹",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom premium theme styles
st.markdown("""
    <style>
    .main {
        background-color: #f8f9fa;
    }
    .kpi-card {
        background-color: white;
        padding: 15px;
        border-radius: 12px;
        box-shadow: 0 4px 15px rgba(0,0,0,0.05);
        border: 1px solid #eef2f5;
        transition: transform 0.2s ease, box-shadow 0.2s ease;
        cursor: pointer;
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        height: 140px;
    }
    .kpi-card:hover {
        transform: translateY(-3px);
        box-shadow: 0 8px 20px rgba(0,0,0,0.08);
        border-color: #007bff;
    }
    .kpi-title {
        font-size: 13px;
        color: #6c757d;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .kpi-value-container {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        margin-top: 5px;
        margin-bottom: 5px;
    }
    .kpi-value {
        font-size: 32px;
        font-weight: 700;
        color: #1e293b;
    }
    .kpi-delta {
        font-size: 12px;
        font-weight: 600;
        margin-left: 8px;
    }
    .delta-up { color: #10b981; }
    .delta-down { color: #ef4444; }
    .sparkline-container {
        height: 35px;
        margin-top: 5px;
    }
    /* Sticky header styles */
    .sticky-header {
        position: -webkit-sticky;
        position: sticky;
        top: 0;
        background-color: #ffffff;
        z-index: 999;
        padding: 10px 0;
        border-bottom: 1px solid #eef2f5;
    }
    </style>
""", unsafe_allow_html=True)

DATA_FILE = "data/final_triage_output.pkl"

if not os.path.exists(DATA_FILE):
    st.error(
        f"### ⚠️ Data Ledger Missing\n"
        f"The file `{DATA_FILE}` was not found. Please run the backend ingestion, extraction, and rules scripts first."
    )
    st.stop()

# Helper: Load and prepare data
@st.cache_data
def load_and_preprocess_data():
    df = pd.read_pickle(DATA_FILE)
    
    # Parse dates
    df["last_modified_at"] = pd.to_datetime(df["last_modified_at"])
    
    # Extract primary wound details
    flat_wounds = []
    for _, row in df.iterrows():
        wounds = row.get("extracted_wounds", [])
        primary_wound = None
        if wounds:
            primary_wounds = [w for w in wounds if w.get("is_primary_wound")]
            primary_wound = primary_wounds[0] if primary_wounds else wounds[0]
            
        dims = "N/A"
        l, w, d = None, None, None
        if primary_wound:
            l = primary_wound.get("length_cm")
            w = primary_wound.get("width_cm")
            d = primary_wound.get("depth_cm")
            if l is not None or w is not None or d is not None:
                dims = f"{l or '-'} x {w or '-'} x {d or '-'} cm"

        flat_wounds.append({
            "Wound Type": primary_wound.get("wound_type", "N/A") if primary_wound else "N/A",
            "Location": primary_wound.get("location", "N/A") if primary_wound else "N/A",
            "Length": l,
            "Width": w,
            "Depth": d,
            "Dimensions": dims,
            "Drainage Level": primary_wound.get("drainage_amount", "N/A") if primary_wound else "N/A",
        })
        
    df_wounds = pd.DataFrame(flat_wounds)
    df = pd.concat([df.reset_index(drop=True), df_wounds], axis=1)
    
    # Standardize triage decision strings
    df["routing_decision"] = df["routing_decision"].str.upper()
    return df

df_raw = load_and_preprocess_data()

# ---------------------------------------------------------
# State Preservation: Initialize session states
# ---------------------------------------------------------
if "triage_filter" not in st.session_state:
    st.session_state.triage_filter = "ALL"
if "selected_facilities" not in st.session_state:
    st.session_state.selected_facilities = []
if "selected_payers" not in st.session_state:
    st.session_state.selected_payers = []
if "date_preset" not in st.session_state:
    st.session_state.date_preset = "All Time"
if "custom_date_range" not in st.session_state:
    st.session_state.custom_date_range = (df_raw["last_modified_at"].min().date(), df_raw["last_modified_at"].max().date())
if "search_query" not in st.session_state:
    st.session_state.search_query = ""
if "temporal_granularity" not in st.session_state:
    st.session_state.temporal_granularity = "Weekly"
if "view_value_mode" not in st.session_state:
    st.session_state.view_value_mode = "Absolute"

# Callback helpers
def set_triage_filter(status):
    st.session_state.triage_filter = status

def reset_filters():
    st.session_state.triage_filter = "ALL"
    st.session_state.selected_facilities = []
    st.session_state.selected_payers = []
    st.session_state.date_preset = "All Time"
    st.session_state.search_query = ""
    st.session_state.custom_date_range = (df_raw["last_modified_at"].min().date(), df_raw["last_modified_at"].max().date())

# ---------------------------------------------------------
# Sidebar - Global Control Pane
# ---------------------------------------------------------
st.sidebar.header("🎛️ Global Control Pane")

# Preset Dates
presets = ["All Time", "Last 30 Days", "Last 14 Days", "Custom"]
selected_preset = st.sidebar.selectbox("Date Window Preset", options=presets, key="date_preset")

# Custom Date Range Picker (enabled if preset is Custom)
min_date = df_raw["last_modified_at"].min().date()
max_date = df_raw["last_modified_at"].max().date()

if selected_preset == "Custom":
    start_date, end_date = st.sidebar.date_input(
        "Custom Range",
        value=st.session_state.custom_date_range,
        min_value=min_date,
        max_value=max_date
    )
    st.session_state.custom_date_range = (start_date, end_date)
elif selected_preset == "Last 30 Days":
    start_date = max_date - timedelta(days=30)
    end_date = max_date
elif selected_preset == "Last 14 Days":
    start_date = max_date - timedelta(days=14)
    end_date = max_date
else: # All Time
    start_date = min_date
    end_date = max_date

# Facility Filter
facilities_list = sorted([f"Facility {fid}" for fid in df_raw["facility_id"].unique()])
selected_facs = st.sidebar.multiselect(
    "Facilities",
    options=facilities_list,
    key="selected_facilities"
)

# Payer Filter
payers_list = sorted(df_raw["primary_payer_code"].dropna().unique())
selected_pyrs = st.sidebar.multiselect(
    "Payers",
    options=payers_list,
    key="selected_payers"
)

# Search input
search_in = st.sidebar.text_input("🔍 Search (Name, ID, Reason)", key="search_query")

if st.sidebar.button("Reset All Filters", on_click=reset_filters):
    st.rerun()

# ---------------------------------------------------------
# Filter Application Logic
# ---------------------------------------------------------
df_filtered = df_raw.copy()

# Apply Date Filter
df_filtered = df_filtered[
    (df_filtered["last_modified_at"].dt.date >= start_date) & 
    (df_filtered["last_modified_at"].dt.date <= end_date)
]

# Apply Facility Filter
if selected_facs:
    fac_ids = [int(f.replace("Facility ", "")) for f in selected_facs]
    df_filtered = df_filtered[df_filtered["facility_id"].isin(fac_ids)]

# Apply Payer Filter
if selected_pyrs:
    df_filtered = df_filtered[df_filtered["primary_payer_code"].isin(selected_pyrs)]

# Apply Text Search Filter
if search_in:
    search_lower = search_in.lower()
    df_filtered = df_filtered[
        df_filtered["first_name"].str.lower().str.contains(search_lower, na=False) |
        df_filtered["last_name"].str.lower().str.contains(search_lower, na=False) |
        df_filtered["patient_id"].str.lower().str.contains(search_lower, na=False) |
        df_filtered["reason"].str.lower().str.contains(search_lower, na=False)
    ]

# Apply Clickable KPI Ribbon Filter
if st.session_state.triage_filter != "ALL":
    df_filtered = df_filtered[df_filtered["routing_decision"] == st.session_state.triage_filter]

# ---------------------------------------------------------
# Micro-Sparkline Chart Helper
# ---------------------------------------------------------
def make_sparkline_chart(series_data, color):
    fig = go.Figure(go.Scatter(
        y=series_data,
        mode="lines",
        line=dict(color=color, width=2.5),
        fill="tozeroy",
        fillcolor=f"rgba(16, 185, 129, 0.1)" if color == "#10b981" else f"rgba(0, 123, 255, 0.1)"
    ))
    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        showlegend=False,
        height=35,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)"
    )
    return fig

# ---------------------------------------------------------
# KPI Ribbon calculation
# ---------------------------------------------------------
# Calculate counts from the currently filtered dataset (excluding the KPI status filter itself to allow toggle)
df_kpi_base = df_raw.copy()
df_kpi_base = df_kpi_base[
    (df_kpi_base["last_modified_at"].dt.date >= start_date) & 
    (df_kpi_base["last_modified_at"].dt.date <= end_date)
]
if selected_facs:
    fac_ids = [int(f.replace("Facility ", "")) for f in selected_facs]
    df_kpi_base = df_kpi_base[df_kpi_base["facility_id"].isin(fac_ids)]
if selected_pyrs:
    df_kpi_base = df_kpi_base[df_kpi_base["primary_payer_code"].isin(selected_pyrs)]
if search_in:
    search_lower = search_in.lower()
    df_kpi_base = df_kpi_base[
        df_kpi_base["first_name"].str.lower().str.contains(search_lower, na=False) |
        df_kpi_base["last_name"].str.lower().str.contains(search_lower, na=False) |
        df_kpi_base["patient_id"].str.lower().str.contains(search_lower, na=False) |
        df_kpi_base["reason"].str.lower().str.contains(search_lower, na=False)
    ]

total_cnt = len(df_kpi_base)
accept_cnt = len(df_kpi_base[df_kpi_base["routing_decision"] == "AUTO_ACCEPT"])
review_cnt = len(df_kpi_base[df_kpi_base["routing_decision"] == "FLAG_FOR_REVIEW"])
reject_cnt = len(df_kpi_base[df_kpi_base["routing_decision"] == "REJECT"])
accept_rate = (accept_cnt / total_cnt * 100) if total_cnt > 0 else 0

# Mock trends for sparklines (grouped by last 7 days of the date window)
trend_dates = pd.date_range(end=end_date, periods=7).date
trend_total = []
trend_accept = []
trend_review = []
trend_reject = []

for d in trend_dates:
    day_df = df_kpi_base[df_kpi_base["last_modified_at"].dt.date == d]
    trend_total.append(len(day_df))
    trend_accept.append(len(day_df[day_df["routing_decision"] == "AUTO_ACCEPT"]))
    trend_review.append(len(day_df[day_df["routing_decision"] == "FLAG_FOR_REVIEW"]))
    trend_reject.append(len(day_df[day_df["routing_decision"] == "REJECT"]))

# ---------------------------------------------------------
# Header & Navigation
# ---------------------------------------------------------
st.title("🩹 Medicare Part B Wound Care Billing Triage")
st.markdown("Automated clinical extraction and compliance analytics dashboard.")

# ---------------------------------------------------------
# KPI Summary Ribbon (Clickable for Drill-Down)
# ---------------------------------------------------------
kpi_cols = st.columns(5)

with kpi_cols[0]:
    # Total Patients Card
    is_active = st.session_state.triage_filter == "ALL"
    border_style = "border-left: 5px solid #007bff;" if is_active else ""
    st.markdown(
        f'<div class="kpi-card" style="{border_style}">'
        f'<div class="kpi-title">Total Patients</div>'
        f'<div class="kpi-value-container">'
        f'<div class="kpi-value">{total_cnt}</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True
    )
    if st.button("Focus: All Patients", key="btn_all", use_container_width=True):
        set_triage_filter("ALL")
        st.rerun()
    st.plotly_chart(make_sparkline_chart(trend_total, "#007bff"), use_container_width=True, key="spark_total", config={'displayModeBar': False})

with kpi_cols[1]:
    # Claims Approved Card
    is_active = st.session_state.triage_filter == "AUTO_ACCEPT"
    border_style = "border-left: 5px solid #10b981;" if is_active else ""
    st.markdown(
        f'<div class="kpi-card" style="{border_style}">'
        f'<div class="kpi-title">Approved (Auto-Accept)</div>'
        f'<div class="kpi-value-container">'
        f'<div class="kpi-value" style="color: #10b981;">{accept_cnt}</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True
    )
    if st.button("Focus: Approved", key="btn_accept", use_container_width=True):
        set_triage_filter("AUTO_ACCEPT")
        st.rerun()
    st.plotly_chart(make_sparkline_chart(trend_accept, "#10b981"), use_container_width=True, key="spark_accept", config={'displayModeBar': False})

with kpi_cols[2]:
    # Pending Review Card
    is_active = st.session_state.triage_filter == "FLAG_FOR_REVIEW"
    border_style = "border-left: 5px solid #f59e0b;" if is_active else ""
    st.markdown(
        f'<div class="kpi-card" style="{border_style}">'
        f'<div class="kpi-title">Pending Review</div>'
        f'<div class="kpi-value-container">'
        f'<div class="kpi-value" style="color: #f59e0b;">{review_cnt}</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True
    )
    if st.button("Focus: Pending", key="btn_review", use_container_width=True):
        set_triage_filter("FLAG_FOR_REVIEW")
        st.rerun()
    st.plotly_chart(make_sparkline_chart(trend_review, "#f59e0b"), use_container_width=True, key="spark_review", config={'displayModeBar': False})

with kpi_cols[3]:
    # Ineligible Card
    is_active = st.session_state.triage_filter == "REJECT"
    border_style = "border-left: 5px solid #ef4444;" if is_active else ""
    st.markdown(
        f'<div class="kpi-card" style="{border_style}">'
        f'<div class="kpi-title">Ineligible (Reject)</div>'
        f'<div class="kpi-value-container">'
        f'<div class="kpi-value" style="color: #ef4444;">{reject_cnt}</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True
    )
    if st.button("Focus: Ineligible", key="btn_reject", use_container_width=True):
        set_triage_filter("REJECT")
        st.rerun()
    st.plotly_chart(make_sparkline_chart(trend_reject, "#ef4444"), use_container_width=True, key="spark_reject", config={'displayModeBar': False})

with kpi_cols[4]:
    # Acceptance Rate Card
    st.markdown(
        f'<div class="kpi-card" style="border-left: 5px solid #8b5cf6;">'
        f'<div class="kpi-title">Acceptance Rate</div>'
        f'<div class="kpi-value-container">'
        f'<div class="kpi-value" style="color: #8b5cf6;">{accept_rate:.1f}%</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True
    )
    st.caption("Auto-approved claims out of total evaluated Medicare B patients in this window.")

# ---------------------------------------------------------
# Primary Viewport: Charts & Trends
# ---------------------------------------------------------
st.write("---")

view_col1, view_col2 = st.columns([2, 1])

with view_col1:
    st.subheader("📈 Triage Trend Over Time")
    
    # Controls for the temporal chart
    ctrl_col1, ctrl_col2, ctrl_col3 = st.columns(3)
    with ctrl_col1:
        granularity = st.radio(
            "Temporal Granularity",
            options=["Daily", "Weekly", "Monthly"],
            key="temporal_granularity",
            horizontal=True
        )
    with ctrl_col2:
        val_mode = st.radio(
            "Value Mode",
            options=["Absolute", "Percentage Composition"],
            key="view_value_mode",
            horizontal=True
        )
        
    # Group data by time
    df_trend = df_filtered.copy()
    if granularity == "Daily":
        df_trend["Period"] = df_trend["last_modified_at"].dt.to_period("D").dt.start_time
    elif granularity == "Weekly":
        df_trend["Period"] = df_trend["last_modified_at"].dt.to_period("W").dt.start_time
    else:
        df_trend["Period"] = df_trend["last_modified_at"].dt.to_period("M").dt.start_time
        
    trend_grouped = df_trend.groupby(["Period", "routing_decision"]).size().unstack(fill_value=0)
    
    # Reindex to ensure all columns exist
    for col in ["AUTO_ACCEPT", "FLAG_FOR_REVIEW", "REJECT"]:
        if col not in trend_grouped.columns:
            trend_grouped[col] = 0
            
    trend_grouped = trend_grouped[["AUTO_ACCEPT", "FLAG_FOR_REVIEW", "REJECT"]]
    
    if val_mode == "Percentage Composition":
        row_totals = trend_grouped.sum(axis=1)
        # Prevent division by zero
        trend_grouped = trend_grouped.div(row_totals.replace(0, 1), axis=0) * 100

    # Build Plotly Chart
    fig_trend = go.Figure()
    colors_map = {
        "AUTO_ACCEPT": "#10b981",
        "FLAG_FOR_REVIEW": "#f59e0b",
        "REJECT": "#ef4444"
    }
    
    for status in ["AUTO_ACCEPT", "FLAG_FOR_REVIEW", "REJECT"]:
        fig_trend.add_trace(go.Scatter(
            x=trend_grouped.index,
            y=trend_grouped[status],
            mode="lines+markers" if len(trend_grouped) < 30 else "lines",
            name=status.replace("_", " ").title(),
            line=dict(color=colors_map[status], width=3),
            stackgroup="one" if val_mode == "Percentage Composition" else None,
            hovertemplate="<b>%{x}</b><br>" + status.title() + ": %{y:.1f}" + ("%" if val_mode == "Percentage Composition" else "") + "<extra></extra>"
        ))
        
    fig_trend.update_layout(
        margin=dict(l=20, r=20, t=10, b=10),
        height=350,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        xaxis=dict(gridcolor="#f1f5f9"),
        yaxis=dict(
            gridcolor="#f1f5f9",
            title="Percentage (%)" if val_mode == "Percentage Composition" else "Count"
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)"
    )
    
    st.plotly_chart(fig_trend, use_container_width=True)

with view_col2:
    st.subheader("🔍 Diagnostics & Variance")
    
    # Diagnostics tabs
    diag_tab1, diag_tab2 = st.tabs(["Why Flagged?", "Payer Mix"])
    
    with diag_tab1:
        # Analyze Flag Reasons
        flagged_df = df_filtered[df_filtered["routing_decision"] == "FLAG_FOR_REVIEW"]
        
        # Extract the reasons
        reasons_series = flagged_df["reason"]
        
        # Parse missing fields using regex
        missing_counts = {
            "Depth Missing": 0,
            "Length Missing": 0,
            "Width Missing": 0,
            "Drainage Level Missing": 0,
            "Multiple Wounds": 0
        }
        
        for r in reasons_series:
            if "Depth" in r:
                missing_counts["Depth Missing"] += 1
            if "Length" in r:
                missing_counts["Length Missing"] += 1
            if "Width" in r:
                missing_counts["Width Missing"] += 1
            if "Drainage" in r:
                missing_counts["Drainage Level Missing"] += 1
            if "Multiple wounds" in r:
                missing_counts["Multiple Wounds"] += 1
                
        df_reasons = pd.DataFrame(list(missing_counts.items()), columns=["Flag Reason", "Count"])
        df_reasons = df_reasons.sort_values(by="Count", ascending=True)
        
        fig_reasons = px.bar(
            df_reasons,
            y="Flag Reason",
            x="Count",
            orientation="h",
            color_discrete_sequence=["#f59e0b"],
            text="Count"
        )
        fig_reasons.update_layout(
            margin=dict(l=10, r=10, t=10, b=10),
            height=280,
            xaxis=dict(visible=False),
            yaxis=dict(title=None),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)"
        )
        st.plotly_chart(fig_reasons, use_container_width=True, config={'displayModeBar': False})
        
    with diag_tab2:
        # Payer Mix Composition
        payer_counts = df_filtered.groupby("primary_payer_code").size().reset_index(name="Count")
        
        fig_payer = px.bar(
            payer_counts,
            x="primary_payer_code",
            y="Count",
            color="primary_payer_code",
            color_discrete_sequence=px.colors.qualitative.Prism,
            text_auto=True
        )
        fig_payer.update_layout(
            margin=dict(l=10, r=10, t=10, b=10),
            height=280,
            xaxis=dict(title="Payer Code"),
            yaxis=dict(title=None, showticklabels=False),
            showlegend=False,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)"
        )
        st.plotly_chart(fig_payer, use_container_width=True, config={'displayModeBar': False})

# ---------------------------------------------------------
# Granular Insights: Interactive Data Table
# ---------------------------------------------------------
st.write("---")
st.subheader("📋 Interactive Triage Queue")

# View Toggles
table_col1, table_col2 = st.columns([3, 1])
with table_col1:
    st.write(f"Showing **{len(df_filtered)}** records matching the active filters.")
with table_col2:
    view_mode = st.radio(
        "View Mode",
        options=["Triage Table", "Raw Metadata Grid"],
        horizontal=True
    )

# Prepare DataFrame for Display
df_table = pd.DataFrame({
    "Patient ID": df_filtered["patient_id"],
    "Name": df_filtered["first_name"] + " " + df_filtered["last_name"],
    "Facility": df_filtered["facility_id"].apply(lambda x: f"Facility {x}"),
    "Payer": df_filtered["primary_payer_code"],
    "Decision": df_filtered["routing_decision"],
    "Wound Type": df_filtered["Wound Type"],
    "Location": df_filtered["Location"],
    "Dimensions (L x W x D)": df_filtered["Dimensions"],
    "Drainage": df_filtered["Drainage Level"],
    "Justification": df_filtered["reason"],
    "Data Source": df_filtered["source_used"]
})

# Color row or cell function for conditional styling
def style_decision_cell(val):
    if val == "AUTO_ACCEPT":
        return "background-color: #e2f0d9; color: #385723; font-weight: bold;"
    elif val == "FLAG_FOR_REVIEW":
        return "background-color: #fff2cc; color: #7f6000; font-weight: bold;"
    elif val == "REJECT":
        return "background-color: #fce4d6; color: #c65911; font-weight: bold;"
    return ""

if view_mode == "Triage Table":
    # Styled dataframe
    styled_table = df_table.style.map(style_decision_cell, subset=["Decision"])
    st.dataframe(styled_table, use_container_width=True, height=400)
else:
    # Raw Metadata Grid
    st.dataframe(df_filtered, use_container_width=True, height=400)

# CSV Export
csv_data = df_table.to_csv(index=False).encode('utf-8')
st.download_button(
    label="📥 Export Triage Queue to CSV",
    data=csv_data,
    file_name=f"wound_care_triage_{datetime.now().strftime('%Y%md_%H%M%S')}.csv",
    mime="text/csv"
)

# ---------------------------------------------------------
# Patient Detail Deep Dive Panel
# ---------------------------------------------------------
st.write("---")
st.subheader("🔍 Patient Detail Deep Dive")

selected_id = st.selectbox(
    "Select a patient profile to inspect:",
    options=["Select Patient..."] + df_filtered["patient_id"].tolist()
)

if selected_id != "Select Patient...":
    pat_row = df_filtered[df_filtered["patient_id"] == selected_id].iloc[0]
    
    detail_cols = st.columns(3)
    
    with detail_cols[0]:
        st.markdown("### 👤 Patient Information")
        st.markdown(f"**Full Name:** {pat_row['first_name']} {pat_row['last_name']}")
        st.markdown(f"**Patient ID:** `{pat_row['patient_id']}`")
        st.markdown(f"**Date of Birth:** {pat_row['birth_date']}")
        st.markdown(f"**Gender:** {pat_row['gender']}")
        st.markdown(f"**Facility:** Facility {pat_row['facility_id']}")
        
    with detail_cols[1]:
        st.markdown("### 🧾 Insurance & Billing Status")
        st.markdown(f"**Primary Payer:** `{pat_row['primary_payer_code']}`")
        st.markdown(f"**Routing Decision:** `{pat_row['routing_decision']}`")
        st.markdown(f"**Justification:** {pat_row['reason']}")
        st.markdown(f"**Source Processed:** `{pat_row['source_used']}`")
        
    with detail_cols[2]:
        st.markdown("### 🩹 Clinical Wound Assessment")
        st.markdown(f"**Wound Type:** {pat_row['Wound Type']}")
        st.markdown(f"**Location:** {pat_row['Location']}")
        st.markdown(f"**Measurements:** {pat_row['Dimensions']}")
        st.markdown(f"**Drainage level:** {pat_row['Drainage Level']}")

    # Diagnoses & History Tab
    diag_exp = st.expander("📁 View Diagnoses & Coverage Log", expanded=False)
    with diag_exp:
        history_col1, history_col2 = st.columns(2)
        with history_col1:
            st.markdown("**ICD-10 Diagnoses on Record:**")
            diag_list = pat_row.get("diagnoses", [])
            if diag_list:
                for d in diag_list:
                    st.markdown(f"- `{d.get('icd10_code')}`: {d.get('icd10_description')} ({d.get('clinical_status')})")
            else:
                st.markdown("*No diagnoses recorded.*")
        with history_col2:
            st.markdown("**Coverage Policies:**")
            cov_list = pat_row.get("coverage", [])
            if cov_list:
                for c in cov_list:
                    eff_to = c.get('effective_to') or 'Active'
                    st.markdown(f"- **{c.get('payer_name')}** ({c.get('payer_code')}): Effective {c.get('effective_from')} to {eff_to}")
            else:
                st.markdown("*No coverage records found.*")
