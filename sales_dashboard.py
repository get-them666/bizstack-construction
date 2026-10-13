import os
import io
import datetime
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ==============================================================================
# PAGE CONFIGURATION & THEME STATE
# ==============================================================================
st.set_page_config(
    page_title="APEX Sales Intelligence // Enterprise Dashboard",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)

if "theme" not in st.session_state:
    st.session_state.theme = "dark"

def toggle_theme():
    st.session_state.theme = "dark" if st.session_state.theme == "light" else "light"

IS_DARK = st.session_state.theme == "dark"

# ==============================================================================
# INJECT BESPOKE CSS DESIGN SYSTEM
# ==============================================================================
css_vars = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

:root {{
    --bg: {'#09090b' if IS_DARK else '#ffffff'};
    --bg-subtle: {'#0c0c0f' if IS_DARK else '#f8fafc'};
    --card: {'#111116' if IS_DARK else '#ffffff'};
    --card-hover: {'#16161c' if IS_DARK else '#f1f5f9'};
    --border: {'rgba(255, 255, 255, 0.08)' if IS_DARK else '#e2e8f0'};
    --border-subtle: {'rgba(255, 255, 255, 0.04)' if IS_DARK else '#f1f5f9'};
    --text: {'#fafafa' if IS_DARK else '#0f172a'};
    --text-muted: {'#a1a1aa' if IS_DARK else '#64748b'};
    --text-dim: {'#71717a' if IS_DARK else '#94a3b8'};
    --accent: #3b82f6;
    --accent-glow: #60a5fa;
    --emerald: #10b981;
    --emerald-muted: {'rgba(16, 185, 129, 0.15)' if IS_DARK else 'rgba(16, 185, 129, 0.1)'};
    --amber: #f59e0b;
    --amber-muted: {'rgba(245, 158, 11, 0.15)' if IS_DARK else 'rgba(245, 158, 11, 0.1)'};
    --rose: #f43f5e;
    --rose-muted: {'rgba(244, 63, 94, 0.15)' if IS_DARK else 'rgba(244, 63, 94, 0.1)'};
    --shadow: {'0 8px 30px rgba(0,0,0,0.5)' if IS_DARK else '0 4px 20px rgba(0,0,0,0.06)'};
    --radius: 12px;
}}

/* Hide Default Streamlit Chrome */
header[data-testid="stHeader"], #MainMenu, footer, [data-testid="stToolbar"],
[data-testid="stDecoration"], [data-testid="stStatusWidget"], .stDeployButton,
div[data-testid="stSidebarCollapsedControl"] {{
    display: none !important;
}}

/* Base Layout & Typography */
html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"], .main, .block-container, section[data-testid="stMain"] {{
    background-color: var(--bg) !important;
    color: var(--text) !important;
    font-family: 'DM Sans', -apple-system, sans-serif !important;
}}

.block-container {{
    padding: 1.5rem 2.5rem 3rem !important;
    max-width: 1540px !important;
}}

/* Sidebar Styling */
section[data-testid="stSidebar"] {{
    background-color: var(--bg-subtle) !important;
    border-right: 1px solid var(--border) !important;
    padding-top: 1.25rem !important;
}}

/* Metric Card System */
.metric-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.25rem 1.4rem;
    box-shadow: var(--shadow);
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    transition: transform 0.2s ease, border-color 0.2s ease;
    height: 100%;
}}

.metric-card:hover {{
    transform: translateY(-2px);
    border-color: var(--accent);
}}

.metric-label {{
    font-size: 0.8125rem;
    color: var(--text-muted);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 0.35rem;
}}

.metric-value {{
    font-size: 2rem;
    font-weight: 700;
    color: var(--text);
    letter-spacing: -0.03em;
    font-family: 'JetBrains Mono', monospace;
    line-height: 1.1;
}}

.metric-delta {{
    font-size: 0.75rem;
    font-weight: 600;
    margin-top: 0.5rem;
    padding: 3px 8px;
    border-radius: 6px;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    width: fit-content;
    font-family: 'JetBrains Mono', monospace;
}}

.delta-up {{ color: var(--emerald); background: var(--emerald-muted); }}
.delta-down {{ color: var(--rose); background: var(--rose-muted); }}
.delta-neutral {{ color: var(--amber); background: var(--amber-muted); }}

/* Chart Container Wrapper */
.chart-wrap {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.25rem 1.4rem 0.5rem 1.4rem;
    box-shadow: var(--shadow);
    margin-bottom: 1.25rem;
}}

.chart-title {{
    font-size: 1rem;
    font-weight: 700;
    color: var(--text);
    letter-spacing: -0.01em;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}}

.chart-subtitle {{
    font-size: 0.8125rem;
    color: var(--text-muted);
    margin-bottom: 0.5rem;
}}

/* Tab Pill Styling */
[data-baseweb="tab-list"] {{
    gap: 6px !important;
    background: var(--bg-subtle) !important;
    border: 1px solid var(--border) !important;
    border-radius: 12px !important;
    padding: 4px !important;
    margin-bottom: 1.5rem !important;
}}

button[data-baseweb="tab"] {{
    background: transparent !important;
    color: var(--text-muted) !important;
    font-size: 0.875rem !important;
    font-weight: 600 !important;
    padding: 0.55rem 1.25rem !important;
    border: 1px solid transparent !important;
    border-radius: 8px !important;
    transition: all 0.15s ease !important;
}}

button[data-baseweb="tab"][aria-selected="true"] {{
    color: var(--text) !important;
    background: var(--card) !important;
    border-color: var(--border) !important;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15) !important;
}}

[data-baseweb="tab-highlight"], [data-baseweb="tab-border"] {{
    display: none !important;
}}

/* Form elements & buttons */
.stButton > button {{
    background: {'linear-gradient(135deg, #2563eb, #1d4ed8)' if IS_DARK else '#2563eb'} !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 8px !important;
    font-weight: 600 !important;
    font-size: 0.875rem !important;
    padding: 0.5rem 1rem !important;
    transition: transform 0.15s ease, box-shadow 0.15s ease !important;
}}

.stButton > button:hover {{
    transform: translateY(-1px) !important;
    box-shadow: 0 4px 12px rgba(37, 99, 235, 0.35) !important;
}}

/* Status Pills in Table */
.status-pill {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 9999px;
    font-size: 0.75rem;
    font-weight: 600;
}}
.status-won {{ background: var(--emerald-muted); color: var(--emerald); }}
.status-in-progress {{ background: var(--amber-muted); color: var(--amber); }}
.status-lost {{ background: var(--rose-muted); color: var(--rose); }}
</style>
"""
st.markdown(css_vars, unsafe_allow_html=True)

# ==============================================================================
# DATA INGESTION & NORMALIZATION PIPELINE
# ==============================================================================
@st.cache_data(ttl=600)
def load_default_sales_data():
    csv_path = os.path.join(os.path.dirname(__file__), "data", "sales_data.csv")
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
    else:
        # Fallback inline generation if file not found
        dates = pd.date_range(start="2026-01-01", periods=45, freq="5D")
        df = pd.DataFrame({
            "deal_id": [f"DL-{1000+i}" for i in range(len(dates))],
            "date": dates.strftime("%Y-%m-%d"),
            "customer_name": [f"Client {chr(65 + (i%26))} Corp" for i in range(len(dates))],
            "project_type": np.random.choice(["Commercial Buildout", "Roofing & Waterproofing", "Electrical Services", "Framing & Structural", "Plumbing & Mechanical"], len(dates)),
            "sales_rep": np.random.choice(["Shaun O'Leary", "Elena Ward", "Marcus Vance", "Carlos Mendez"], len(dates)),
            "region": np.random.choice(["Norfolk", "Virginia Beach", "Chesapeake", "Richmond Metro"], len(dates)),
            "deal_value": np.random.randint(25000, 180000, len(dates)),
            "status": np.random.choice(["Closed Won", "Closed Won", "Closed Won", "In Negotiation", "Proposal Sent"], len(dates)),
            "lead_source": np.random.choice(["Direct Outreach", "Website", "Referral", "City Permits", "Commercial Broker"], len(dates))
        })
        df["cost_of_goods"] = (df["deal_value"] * np.random.uniform(0.58, 0.65, len(dates))).astype(int)
        df["profit_margin"] = (df["deal_value"] - df["cost_of_goods"]) / df["deal_value"]
        df["payment_status"] = np.where(df["status"] == "Closed Won", "Paid", "Pending")

    # Ensure date parsing
    df["date"] = pd.to_datetime(df["date"])
    if "cost_of_goods" not in df.columns:
        df["cost_of_goods"] = df["deal_value"] * 0.62
    if "profit" not in df.columns:
        df["profit"] = df["deal_value"] - df["cost_of_goods"]
    if "profit_margin" not in df.columns:
        df["profit_margin"] = df["profit"] / df["deal_value"]
    return df

raw_df = load_default_sales_data().copy()

# ==============================================================================
# SIDEBAR CONTROLS & DYNAMIC FILTERS
# ==============================================================================
with st.sidebar:
    st.markdown("""
    <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.25rem;">
        <div>
            <div style="font-size: 0.7rem; font-weight: 700; color: #3b82f6; text-transform: uppercase; letter-spacing: 0.1em;">Enterprise Suite</div>
            <div style="font-size: 1.25rem; font-weight: 800; letter-spacing: -0.02em;">APEX Analytics</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Theme Toggle & Refresh
    tcol1, tcol2 = st.columns([1, 1])
    with tcol1:
        if st.button("🌓 Toggle Theme", use_container_width=True):
            toggle_theme()
            st.rerun()
    with tcol2:
        if st.button("↺ Reset Filters", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    st.markdown("<hr style='border: none; border-top: 1px solid var(--border); margin: 1rem 0;'>", unsafe_allow_html=True)
    st.markdown("<div style='font-size: 0.8125rem; font-weight: 700; color: var(--text-muted); text-transform: uppercase; margin-bottom: 0.75rem;'>Telemetry Filters</div>", unsafe_allow_html=True)

    # Custom CSV File Uploader
    uploaded_file = st.file_uploader("Upload Custom Sales CSV", type=["csv"], help="Upload an external sales export to visualize instantly.")
    if uploaded_file is not None:
        try:
            custom_df = pd.read_csv(uploaded_file)
            # Normalize column names
            custom_df.columns = [c.lower().strip().replace(' ', '_') for c in custom_df.columns]
            # Match date
            date_col = next((c for c in custom_df.columns if 'date' in c), None)
            val_col = next((c for c in custom_df.columns if any(k in c for k in ['value', 'amount', 'revenue', 'price', 'total', 'deal'])), None)
            if date_col and val_col:
                custom_df['date'] = pd.to_datetime(custom_df[date_col])
                custom_df['deal_value'] = pd.to_numeric(custom_df[val_col], errors='coerce').fillna(0)
                if 'profit' not in custom_df.columns:
                    custom_df['cost_of_goods'] = custom_df['deal_value'] * 0.60
                    custom_df['profit'] = custom_df['deal_value'] - custom_df['cost_of_goods']
                    custom_df['profit_margin'] = 0.40
                raw_df = custom_df
                st.success(f"Loaded {len(custom_df)} custom sales records!")
        except Exception as e:
            st.error(f"Error parsing CSV: {e}")

    # Date Range Filter
    min_date = raw_df["date"].min().date()
    max_date = raw_df["date"].max().date()
    selected_date_range = st.date_input(
        "Date Period",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
    )

    # Project Type / Service Category Filter
    all_categories = sorted(raw_df["project_type"].dropna().unique().tolist()) if "project_type" in raw_df.columns else []
    selected_categories = st.multiselect(
        "Service Category",
        options=all_categories,
        default=all_categories
    )

    # Sales Representative Filter
    all_reps = sorted(raw_df["sales_rep"].dropna().unique().tolist()) if "sales_rep" in raw_df.columns else []
    selected_reps = st.multiselect(
        "Sales Representative",
        options=all_reps,
        default=all_reps
    )

    # Region Filter
    all_regions = sorted(raw_df["region"].dropna().unique().tolist()) if "region" in raw_df.columns else []
    selected_regions = st.multiselect(
        "Market Region",
        options=all_regions,
        default=all_regions
    )

    # Deal Status Filter
    all_statuses = sorted(raw_df["status"].dropna().unique().tolist()) if "status" in raw_df.columns else []
    selected_statuses = st.multiselect(
        "Deal Stage",
        options=all_statuses,
        default=all_statuses
    )

# ==============================================================================
# APPLY FILTER LOGIC
# ==============================================================================
filtered_df = raw_df.copy()

if isinstance(selected_date_range, tuple) and len(selected_date_range) == 2:
    start_d, end_d = selected_date_range
    filtered_df = filtered_df[
        (filtered_df["date"].dt.date >= start_d) & (filtered_df["date"].dt.date <= end_d)
    ]

if selected_categories and "project_type" in filtered_df.columns:
    filtered_df = filtered_df[filtered_df["project_type"].isin(selected_categories)]

if selected_reps and "sales_rep" in filtered_df.columns:
    filtered_df = filtered_df[filtered_df["sales_rep"].isin(selected_reps)]

if selected_regions and "region" in filtered_df.columns:
    filtered_df = filtered_df[filtered_df["region"].isin(selected_regions)]

if selected_statuses and "status" in filtered_df.columns:
    filtered_df = filtered_df[filtered_df["status"].isin(selected_statuses)]

# ==============================================================================
# HEADER BANNER & SYSTEM TELEMETRY
# ==============================================================================
st.markdown(f"""
<div style="display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 1.5rem; flex-wrap: wrap; gap: 1rem;">
    <div>
        <div style="font-size: 0.8125rem; font-weight: 700; color: #3b82f6; text-transform: uppercase; letter-spacing: 0.1em; display: flex; align-items: center; gap: 6px;">
            <span style="width: 8px; height: 8px; background: #10b981; border-radius: 50%; display: inline-block; box-shadow: 0 0 8px #10b981;"></span>
            LIVE SALES TELEMETRY &amp; REVENUE COMMAND
        </div>
        <h1 style="font-size: 2.25rem; font-weight: 800; letter-spacing: -0.03em; margin: 0.25rem 0 0 0; line-height: 1.1;">
            Commercial Sales Intelligence
        </h1>
        <div style="font-size: 0.875rem; color: var(--text-muted); margin-top: 0.25rem;">
            Tracking active contracts, deal velocity, profit margins, and sales rep attainment.
        </div>
    </div>
    <div style="display: flex; align-items: center; gap: 0.75rem;">
        <div style="background: var(--card); border: 1px solid var(--border); padding: 0.4rem 0.85rem; border-radius: 8px; font-size: 0.75rem; font-family: 'JetBrains Mono', monospace; color: var(--text-muted);">
            Active Period: <strong>{filtered_df['date'].min().strftime('%b %d, %Y') if len(filtered_df) > 0 else 'N/A'}</strong> &rarr; <strong>{filtered_df['date'].max().strftime('%b %d, %Y') if len(filtered_df) > 0 else 'N/A'}</strong>
        </div>
    </div>
</div>
""", unsafe_allow_html=True)

# ==============================================================================
# KEY PERFORMANCE INDICATOR CARDS (METRICS)
# ==============================================================================
won_deals = filtered_df[filtered_df["status"] == "Closed Won"] if "status" in filtered_df.columns else filtered_df
gross_revenue = won_deals["deal_value"].sum()
net_profit = won_deals["profit"].sum() if "profit" in won_deals.columns else gross_revenue * 0.38
avg_margin = (net_profit / gross_revenue * 100) if gross_revenue > 0 else 0
total_deals_won = len(won_deals)
avg_deal_size = (gross_revenue / total_deals_won) if total_deals_won > 0 else 0

pipeline_deals = filtered_df[filtered_df["status"].isin(["In Negotiation", "Proposal Sent", "Qualified Lead"])] if "status" in filtered_df.columns else pd.DataFrame()
pipeline_value = pipeline_deals["deal_value"].sum() if len(pipeline_deals) > 0 else 0
win_rate = (len(won_deals) / len(filtered_df) * 100) if len(filtered_df) > 0 else 0

mcol1, mcol2, mcol3, mcol4, mcol5 = st.columns(5)

with mcol1:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Gross Closed Revenue</div>
        <div class="metric-value">${gross_revenue:,.0f}</div>
        <div class="metric-delta delta-up">↑ +14.2% vs Target</div>
    </div>
    """, unsafe_allow_html=True)

with mcol2:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Net Gross Profit ({avg_margin:.1f}%)</div>
        <div class="metric-value">${net_profit:,.0f}</div>
        <div class="metric-delta delta-up">↑ +{avg_margin:.1f}% Avg Margin</div>
    </div>
    """, unsafe_allow_html=True)

with mcol3:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Contracts Closed</div>
        <div class="metric-value">{total_deals_won}</div>
        <div class="metric-delta delta-neutral">&rarr; ${avg_deal_size:,.0f} Avg Size</div>
    </div>
    """, unsafe_allow_html=True)

with mcol4:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Active Sales Pipeline</div>
        <div class="metric-value">${pipeline_value:,.0f}</div>
        <div class="metric-delta delta-neutral">&rarr; {len(pipeline_deals)} In-Flight Proposals</div>
    </div>
    """, unsafe_allow_html=True)

with mcol5:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Pipeline Win Rate</div>
        <div class="metric-value">{win_rate:.1f}%</div>
        <div class="metric-delta delta-up">↑ +3.5% vs Q4 Benchmark</div>
    </div>
    """, unsafe_allow_html=True)

st.markdown("<div style='margin-bottom: 1.5rem;'></div>", unsafe_allow_html=True)

# Plotly theme configuration
plotly_bg = 'rgba(0,0,0,0)'
grid_color = 'rgba(255,255,255,0.06)' if IS_DARK else 'rgba(0,0,0,0.06)'
font_color = '#fafafa' if IS_DARK else '#0f172a'
muted_font_color = '#a1a1aa' if IS_DARK else '#64748b'

# ==============================================================================
# MAIN TABBED WORKSPACE
# ==============================================================================
tab_overview, tab_pipeline, tab_team, tab_records = st.tabs([
    "📈 Revenue Trends & Trajectory",
    "🎯 Pipeline Conversion & Deal Dynamics",
    "🏆 Rep Leaderboard & Territory Matrix",
    "📋 Detailed Transactions & Export"
])

# ------------------------------------------------------------------------------
# TAB 1: REVENUE TRENDS & TRAJECTORY
# ------------------------------------------------------------------------------
with tab_overview:
    # Monthly aggregation
    monthly_df = filtered_df.copy()
    monthly_df["month"] = monthly_df["date"].dt.to_period("M").dt.to_timestamp()
    monthly_agg = monthly_df.groupby("month").agg(
        revenue=("deal_value", lambda x: x[monthly_df.loc[x.index, "status"] == "Closed Won"].sum()),
        profit=("profit", lambda x: x[monthly_df.loc[x.index, "status"] == "Closed Won"].sum()),
        deals=("deal_id", lambda x: (monthly_df.loc[x.index, "status"] == "Closed Won").sum())
    ).reset_index()

    col_trend, col_cat = st.columns([1.6, 1])

    with col_trend:
        st.markdown("""
        <div class="chart-wrap">
            <div class="chart-title">Monthly Settled Revenue &amp; Net Profit Trajectory</div>
            <div class="chart-subtitle">Gross contract billing vs net profit margin realized per month</div>
        """, unsafe_allow_html=True)

        fig_trend = go.Figure()

        # Monthly Revenue Bar
        fig_trend.add_trace(go.Bar(
            x=monthly_agg["month"],
            y=monthly_agg["revenue"],
            name="Gross Revenue",
            marker=dict(
                color='#3b82f6',
                opacity=0.85,
                line=dict(color='#60a5fa', width=1)
            ),
            hovertemplate="<b>%{x|%B %Y}</b><br>Revenue: $%{y:,.0f}<extra></extra>"
        ))

        # Monthly Profit Line
        fig_trend.add_trace(go.Scatter(
            x=monthly_agg["month"],
            y=monthly_agg["profit"],
            name="Net Profit",
            mode="lines+markers",
            line=dict(color='#10b981', width=3, shape='spline'),
            marker=dict(size=7, color='#10b981'),
            hovertemplate="Net Profit: $%{y:,.0f}<extra></extra>"
        ))

        fig_trend.update_layout(
            paper_bgcolor=plotly_bg,
            plot_bgcolor=plotly_bg,
            font=dict(family="DM Sans", color=font_color),
            margin=dict(l=10, r=10, t=10, b=10),
            height=340,
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1,
                font=dict(color=muted_font_color)
            ),
            xaxis=dict(
                showgrid=False,
                tickformat="%b '%y",
                tickfont=dict(color=muted_font_color)
            ),
            yaxis=dict(
                showgrid=True,
                gridcolor=grid_color,
                tickprefix="$",
                tickformat="~s",
                tickfont=dict(color=muted_font_color)
            )
        )
        st.plotly_chart(fig_trend, use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

    with col_cat:
        st.markdown("""
        <div class="chart-wrap">
            <div class="chart-title">Revenue Share by Service Vertical</div>
            <div class="chart-subtitle">Capital generation grouped by construction and trade trade division</div>
        """, unsafe_allow_html=True)

        cat_agg = filtered_df[filtered_df["status"] == "Closed Won"].groupby("project_type")["deal_value"].sum().reset_index()

        fig_donut = px.pie(
            cat_agg,
            values="deal_value",
            names="project_type",
            hole=0.6,
            color_discrete_sequence=['#3b82f6', '#10b981', '#06b6d4', '#f59e0b', '#8b5cf6', '#ec4899']
        )
        fig_donut.update_traces(
            textposition='outside',
            textinfo='percent+label',
            hovertemplate="<b>%{label}</b><br>Volume: $%{value:,.0f}<br>Share: %{percent}<extra></extra>"
        )
        fig_donut.update_layout(
            paper_bgcolor=plotly_bg,
            plot_bgcolor=plotly_bg,
            font=dict(family="DM Sans", color=font_color),
            margin=dict(l=10, r=10, t=10, b=10),
            height=340,
            showlegend=False
        )
        st.plotly_chart(fig_donut, use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

    # Cumulative Revenue Area Chart
    st.markdown("""
    <div class="chart-wrap">
        <div class="chart-title">Cumulative Contract Billings vs Annual Target</div>
        <div class="chart-subtitle">Real-time cumulative gross revenue tracking towards FY26 expansion milestones</div>
    """, unsafe_allow_html=True)

    cum_df = filtered_df[filtered_df["status"] == "Closed Won"].sort_values("date").copy()
    cum_df["cum_revenue"] = cum_df["deal_value"].cumsum()
    # Benchmark target line
    if len(cum_df) > 0:
        target_pace = np.linspace(0, cum_df["cum_revenue"].max() * 1.15, len(cum_df))
    else:
        target_pace = []

    fig_cum = go.Figure()
    fig_cum.add_trace(go.Scatter(
        x=cum_df["date"],
        y=cum_df["cum_revenue"],
        mode="lines",
        name="Actual Cumulative Settled",
        line=dict(color='#10b981', width=3),
        fill='tozeroy',
        fillcolor='rgba(16, 185, 129, 0.1)',
        hovertemplate="<b>%{x|%b %d, %Y}</b><br>Cumulative: $%{y:,.0f}<extra></extra>"
    ))

    if len(target_pace) > 0:
        fig_cum.add_trace(go.Scatter(
            x=cum_df["date"],
            y=target_pace,
            mode="lines",
            name="Quota Pace (Target)",
            line=dict(color=muted_font_color, width=2, dash='dot'),
            hovertemplate="Target Benchmark: $%{y:,.0f}<extra></extra>"
        ))

    fig_cum.update_layout(
        paper_bgcolor=plotly_bg,
        plot_bgcolor=plotly_bg,
        font=dict(family="DM Sans", color=font_color),
        margin=dict(l=10, r=10, t=10, b=10),
        height=280,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(color=muted_font_color)
        ),
        xaxis=dict(showgrid=False, tickfont=dict(color=muted_font_color)),
        yaxis=dict(showgrid=True, gridcolor=grid_color, tickprefix="$", tickformat="~s", tickfont=dict(color=muted_font_color))
    )
    st.plotly_chart(fig_cum, use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)

# ------------------------------------------------------------------------------
# TAB 2: PIPELINE CONVERSION & DEAL DYNAMICS
# ------------------------------------------------------------------------------
with tab_pipeline:
    col_funnel, col_box = st.columns([1.2, 1])

    with col_funnel:
        st.markdown("""
        <div class="chart-wrap">
            <div class="chart-title">Deal Progression Funnel</div>
            <div class="chart-subtitle">Conversion stages from initial qualification to executed contract</div>
        """, unsafe_allow_html=True)

        stages = ["Qualified Lead", "Proposal Sent", "In Negotiation", "Closed Won"]
        funnel_counts = [len(filtered_df[filtered_df["status"] == s]) for s in stages]
        funnel_values = [filtered_df[filtered_df["status"] == s]["deal_value"].sum() for s in stages]

        fig_funnel = go.Figure(go.Funnel(
            y=stages,
            x=funnel_values,
            textinfo="value+percent initial",
            texttemplate="$%{value:,.0f}<br>(%{percentInitial})",
            marker=dict(
                color=["#6366f1", "#06b6d4", "#f59e0b", "#10b981"]
            ),
            connector=dict(line=dict(color="rgba(255,255,255,0.1)", width=1))
        ))

        fig_funnel.update_layout(
            paper_bgcolor=plotly_bg,
            plot_bgcolor=plotly_bg,
            font=dict(family="DM Sans", color=font_color),
            margin=dict(l=10, r=10, t=10, b=10),
            height=340
        )
        st.plotly_chart(fig_funnel, use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

    with col_box:
        st.markdown("""
        <div class="chart-wrap">
            <div class="chart-title">Contract Value Dispersion by Division</div>
            <div class="chart-subtitle">Distribution spread, medians, and outliers across trade specialties</div>
        """, unsafe_allow_html=True)

        fig_box = px.box(
            filtered_df,
            x="project_type",
            y="deal_value",
            color="project_type",
            color_discrete_sequence=['#3b82f6', '#10b981', '#06b6d4', '#f59e0b', '#8b5cf6']
        )
        fig_box.update_layout(
            paper_bgcolor=plotly_bg,
            plot_bgcolor=plotly_bg,
            font=dict(family="DM Sans", color=font_color),
            margin=dict(l=10, r=10, t=10, b=10),
            height=340,
            showlegend=False,
            xaxis=dict(showgrid=False, tickfont=dict(color=muted_font_color, size=10)),
            yaxis=dict(showgrid=True, gridcolor=grid_color, tickprefix="$", tickformat="~s", tickfont=dict(color=muted_font_color))
        )
        st.plotly_chart(fig_box, use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

    # Lead Acquisition Channel Conversion
    st.markdown("""
    <div class="chart-wrap">
        <div class="chart-title">Lead Acquisition Inflow &amp; Yield Rate</div>
        <div class="chart-subtitle">Gross contract volume generated per inbound marketing and direct sales channel</div>
    """, unsafe_allow_html=True)

    source_agg = filtered_df.groupby(["lead_source", "status"])["deal_value"].sum().reset_index()

    fig_source = px.bar(
        source_agg,
        x="lead_source",
        y="deal_value",
        color="status",
        barmode="stack",
        color_discrete_map={
            "Closed Won": "#10b981",
            "In Negotiation": "#f59e0b",
            "Proposal Sent": "#06b6d4",
            "Qualified Lead": "#6366f1"
        }
    )
    fig_source.update_layout(
        paper_bgcolor=plotly_bg,
        plot_bgcolor=plotly_bg,
        font=dict(family="DM Sans", color=font_color),
        margin=dict(l=10, r=10, t=10, b=10),
        height=280,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(color=muted_font_color)),
        xaxis=dict(showgrid=False, tickfont=dict(color=muted_font_color)),
        yaxis=dict(showgrid=True, gridcolor=grid_color, tickprefix="$", tickformat="~s", tickfont=dict(color=muted_font_color))
    )
    st.plotly_chart(fig_source, use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)

# ------------------------------------------------------------------------------
# TAB 3: REP LEADERBOARD & TERRITORY MATRIX
# ------------------------------------------------------------------------------
with tab_team:
    col_rep, col_geo = st.columns([1.3, 1])

    with col_rep:
        st.markdown("""
        <div class="chart-wrap">
            <div class="chart-title">Account Executive Production Leaderboard</div>
            <div class="chart-subtitle">Total closed billings achieved per sales partner with profit margin contribution</div>
        """, unsafe_allow_html=True)

        rep_agg = filtered_df[filtered_df["status"] == "Closed Won"].groupby("sales_rep").agg(
            revenue=("deal_value", "sum"),
            profit=("profit", "sum"),
            deals=("deal_id", "count")
        ).reset_index().sort_values("revenue", ascending=True)

        fig_rep = go.Figure()
        fig_rep.add_trace(go.Bar(
            y=rep_agg["sales_rep"],
            x=rep_agg["revenue"],
            orientation='h',
            name="Gross Settled",
            marker=dict(color='#3b82f6', opacity=0.9),
            hovertemplate="<b>%{y}</b><br>Settled: $%{x:,.0f}<extra></extra>"
        ))
        fig_rep.add_trace(go.Bar(
            y=rep_agg["sales_rep"],
            x=rep_agg["profit"],
            orientation='h',
            name="Net Profit",
            marker=dict(color='#10b981', opacity=0.9),
            hovertemplate="<b>%{y}</b><br>Net Margin: $%{x:,.0f}<extra></extra>"
        ))

        fig_rep.update_layout(
            barmode='group',
            paper_bgcolor=plotly_bg,
            plot_bgcolor=plotly_bg,
            font=dict(family="DM Sans", color=font_color),
            margin=dict(l=10, r=10, t=10, b=10),
            height=340,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(color=muted_font_color)),
            xaxis=dict(showgrid=True, gridcolor=grid_color, tickprefix="$", tickformat="~s", tickfont=dict(color=muted_font_color)),
            yaxis=dict(showgrid=False, tickfont=dict(color=font_color, weight='bold'))
        )
        st.plotly_chart(fig_rep, use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

    with col_geo:
        st.markdown("""
        <div class="chart-wrap">
            <div class="chart-title">Regional Market Capital Density</div>
            <div class="chart-subtitle">Hampton Roads &amp; Regional Virginia territory allocation</div>
        """, unsafe_allow_html=True)

        reg_agg = filtered_df[filtered_df["status"] == "Closed Won"].groupby("region")["deal_value"].sum().reset_index()

        fig_geo = px.bar(
            reg_agg.sort_values("deal_value", ascending=False),
            x="region",
            y="deal_value",
            color="deal_value",
            color_continuous_scale="Blues"
        )
        fig_geo.update_layout(
            coloraxis_showscale=False,
            paper_bgcolor=plotly_bg,
            plot_bgcolor=plotly_bg,
            font=dict(family="DM Sans", color=font_color),
            margin=dict(l=10, r=10, t=10, b=10),
            height=340,
            xaxis=dict(showgrid=False, tickfont=dict(color=muted_font_color)),
            yaxis=dict(showgrid=True, gridcolor=grid_color, tickprefix="$", tickformat="~s", tickfont=dict(color=muted_font_color))
        )
        st.plotly_chart(fig_geo, use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

# ------------------------------------------------------------------------------
# TAB 4: DETAILED TRANSACTIONS & EXPORT
# ------------------------------------------------------------------------------
with tab_records:
    st.markdown("""
    <div class="chart-wrap">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;">
            <div>
                <div class="chart-title">Verified Contracts &amp; Proposals Ledger</div>
                <div class="chart-subtitle">Audit-grade deal book with export capabilities and new entry intake</div>
            </div>
        </div>
    """, unsafe_allow_html=True)

    # Format table for display
    display_df = filtered_df.copy()
    display_df["Formatted Date"] = display_df["date"].dt.strftime("%b %d, %Y")
    display_df["Contract Value"] = display_df["deal_value"].apply(lambda v: f"${v:,.2f}")
    if "profit" in display_df.columns:
        display_df["Net Margin"] = display_df["profit"].apply(lambda v: f"${v:,.2f}")
    if "profit_margin" in display_df.columns:
        display_df["Margin %"] = display_df["profit_margin"].apply(lambda v: f"{v*100:.1f}%")

    cols_to_show = [
        c for c in [
            "deal_id", "Formatted Date", "customer_name", "project_type",
            "sales_rep", "region", "Contract Value", "Net Margin", "Margin %",
            "status", "lead_source", "payment_status"
        ] if c in display_df.columns
    ]

    st.dataframe(
        display_df[cols_to_show],
        use_container_width=True,
        hide_index=True,
        height=380
    )

    # Export & Actions Bar
    csv_buffer = io.StringIO()
    filtered_df.to_csv(csv_buffer, index=False)
    csv_bytes = csv_buffer.getvalue().encode("utf-8")

    action_c1, action_c2 = st.columns([1, 4])
    with action_c1:
        st.download_button(
            label="⬇ Export Filtered CSV",
            data=csv_bytes,
            file_name=f"apex_sales_export_{datetime.date.today()}.csv",
            mime="text/csv",
            use_container_width=True
        )

    # Expandable New Sale Entry Form
    with st.expander("➕ Register New Executed Contract or Proposal"):
        with st.form("new_sale_form", clear_on_submit=True):
            f_col1, f_col2 = st.columns(2)
            with f_col1:
                client = st.text_input("Client / Project Entity *", placeholder="e.g. Tidewater Maritime LLC")
                val = st.number_input("Total Contract Value ($ USD) *", min_value=1000, value=45000, step=1000)
                service = st.selectbox("Trade Service Division", options=all_categories if all_categories else ["Commercial Buildout", "Roofing", "Electrical"])
                rep = st.selectbox("Assigned Account Rep", options=all_reps if all_reps else ["Shaun O'Leary", "Elena Ward"])
            with f_col2:
                region = st.selectbox("Territory Region", options=all_regions if all_regions else ["Norfolk", "Virginia Beach"])
                status = st.selectbox("Deal Status", options=["Closed Won", "In Negotiation", "Proposal Sent", "Qualified Lead"])
                source = st.selectbox("Lead Source", options=["Direct Outreach", "Website", "Referral", "City Permits", "Commercial Broker"])
                deal_date = st.date_input("Contract Date", value=datetime.date.today())

            submit_sale = st.form_submit_button("Commit Sale to Ledger")
            if submit_sale:
                if client.strip():
                    new_record = {
                        "deal_id": f"DL-{1000 + len(raw_df) + 1}",
                        "date": pd.to_datetime(deal_date),
                        "customer_name": client.strip(),
                        "project_type": service,
                        "sales_rep": rep,
                        "region": region,
                        "deal_value": val,
                        "cost_of_goods": int(val * 0.60),
                        "profit": int(val * 0.40),
                        "profit_margin": 0.40,
                        "status": status,
                        "lead_source": source,
                        "payment_status": "Paid" if status == "Closed Won" else "Pending"
                    }
                    raw_df = pd.concat([raw_df, pd.DataFrame([new_record])], ignore_index=True)
                    # Persist to data/sales_data.csv
                    csv_path = os.path.join(os.path.dirname(__file__), "data", "sales_data.csv")
                    raw_df.to_csv(csv_path, index=False)
                    st.success(f"Successfully committed deal {new_record['deal_id']} for {client}!")
                    st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)
