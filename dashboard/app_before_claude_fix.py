"""
NBA Edge Model — Streamlit Dashboard
Run: streamlit run dashboard/app.py
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from pathlib import Path
import pickle
import sqlite3
from scipy.stats import norm

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "nba_edge.db"
FEATURES_PATH = PROJECT_ROOT / "data" / "processed" / "features.parquet"
MODELS_DIR = PROJECT_ROOT / "data" / "processed" / "models"
BLEND_WEIGHT = 0.65  # Must match player_props.py

STAT_LABELS = {
    "pts": "Points",
    "reb": "Rebounds",
    "ast": "Assists",
}

# Only stats that are calibrated
CALIBRATED_STATS = ["pts", "reb", "ast"]

# Common prop lines per stat (for backtest display)
COMMON_LINES = {
    "pts": [15.5, 17.5, 19.5, 21.5, 23.5, 25.5, 27.5, 29.5],
    "reb": [3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 11.5],
    "ast": [2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5],
}

# ---------------------------------------------------------------------------
# DATA LOADING (cached so it doesn't reload on every interaction)
# ---------------------------------------------------------------------------
@st.cache_data
def load_features():
    """Load the feature parquet file."""
    if not FEATURES_PATH.exists():
        return None
    df = pd.read_parquet(FEATURES_PATH)
    return df


@st.cache_resource
def load_models():
    """Load all trained model pickle files."""
    models = {}
    if not MODELS_DIR.exists():
        return models
    for pkl_file in MODELS_DIR.glob("*.pkl"):
        stat = pkl_file.stem.replace("_model", "")
        with open(pkl_file, "rb") as f:
            models[stat] = pickle.load(f)
    return models


@st.cache_data
def load_player_names():
    """Load player ID -> name mapping from the database."""
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(str(DB_PATH))
    players = pd.read_sql("SELECT player_id, full_name FROM players", conn)
    conn.close()
    return dict(zip(players["player_id"], players["full_name"]))


@st.cache_data
def get_db_summary():
    """Get row counts per season from the database."""
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(DB_PATH))
    summary = pd.read_sql(
        """
        SELECT season, COUNT(*) as games, COUNT(DISTINCT player_id) as players
        FROM game_logs GROUP BY season ORDER BY season
        """,
        conn,
    )
    total = pd.read_sql("SELECT COUNT(*) as n FROM game_logs", conn).iloc[0]["n"]
    conn.close()
    return summary, total


# ---------------------------------------------------------------------------
# BACKTEST ENGINE (mirrors edge_finder.py logic)
# ---------------------------------------------------------------------------
@st.cache_data
def run_backtest(stat, threshold=0.55):
    """
    Run a backtest for a single stat on the 2024-25 test set.
    Returns a DataFrame with columns:
        player_id, game_date, predicted, actual, line, prob_over, hit_over
    """
    df = load_features()
    models = load_models()

    if df is None or stat not in models:
        return pd.DataFrame()

    model = models[stat]
    test = df[df["season"] == "2024-25"].copy()

    if len(test) == 0:
        return pd.DataFrame()

    # Minutes filter (match edge_finder.py)
    l10_col = f"min_L10"
    if l10_col in test.columns:
        test = test[test[l10_col] >= 20].copy()

    if len(test) == 0:
        return pd.DataFrame()

    # Get feature columns (same logic as player_props.py)
    exclude_prefixes = ["player_id", "game_id", "game_date", "season", "matchup",
                        "team_abbr", "opp_abbr", "wl", "season_type", "is_home"]
    target_stats = ["pts", "reb", "ast", "stl", "blk", "tov", "pf",
                    "fgm", "fga", "fg3m", "fg3a", "ftm", "fta",
                    "oreb", "dreb", "plus_minus", "min",
                    "pts_reb", "pts_ast", "reb_ast", "pra"]

    feature_cols = [c for c in test.columns
                    if c not in exclude_prefixes + target_stats
                    and not c.startswith("__")]

    # Filter to columns the model was trained on
    if hasattr(model, "feature_names_in_"):
        trained_features = list(model.feature_names_in_)
        feature_cols = [c for c in trained_features if c in test.columns]
    elif hasattr(model, "n_features_in_"):
        feature_cols = feature_cols[: model.n_features_in_]

    X_test = test[feature_cols].copy()
    X_test = X_test.fillna(X_test.median())

    # Predict
    model_pred = model.predict(X_test)

    # Blend with L10
    l10_col_stat = f"{stat}_L10"
    if l10_col_stat in test.columns:
        l10_vals = test[l10_col_stat].fillna(test[stat]).values
        blended = BLEND_WEIGHT * l10_vals + (1 - BLEND_WEIGHT) * model_pred
    else:
        blended = model_pred

    # Actual values
    actual = test[stat].values

    # For each common line, compute P(over)
    results = []
    std_col = f"{stat}_std_L10"
    if std_col in test.columns:
        player_std = test[std_col].fillna(test[stat].std()).values
    else:
        player_std = np.full(len(test), test[stat].std())

    # Avoid zero std
    player_std = np.maximum(player_std, 0.5)

    for line in COMMON_LINES.get(stat, []):
        prob_over = 1 - norm.cdf(line + 0.5, loc=blended, scale=player_std)
        hit_over = (actual >= line + 0.5).astype(int)

        for i in range(len(test)):
            if prob_over[i] >= threshold:
                results.append({
                    "player_id": test.iloc[i].get("player_id", 0),
                    "game_date": test.iloc[i].get("game_date", ""),
                    "predicted": round(blended[i], 1),
                    "actual": actual[i],
                    "line": line,
                    "prob_over": round(prob_over[i], 3),
                    "hit_over": hit_over[i],
                    "stat": stat,
                })

    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# PAGE CONFIG
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="NBA Edge Model",
    page_icon="🏀",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# SIDEBAR
# ---------------------------------------------------------------------------
st.sidebar.title("NBA Edge Model")
page = st.sidebar.radio(
    "Navigate",
    ["Overview", "Backtest Results", "Calibration", "Player Lookup"],
)

# ---------------------------------------------------------------------------
# PAGE: OVERVIEW
# ---------------------------------------------------------------------------
if page == "Overview":
    st.title("NBA Edge Model")
    st.markdown(
        "End-to-end NBA player props prediction: data ingestion → feature engineering "
        "→ ML models → probability calibration → daily picks."
    )

    # Database summary
    db_info = get_db_summary()
    if db_info:
        summary, total = db_info
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Game Logs", f"{total:,}")
        col2.metric("Seasons", len(summary))
        col3.metric("Features", "158")

        st.subheader("Data by Season")
        st.dataframe(
            summary.rename(columns={"season": "Season", "games": "Games", "players": "Players"}),
            use_container_width=True,
            hide_index=True,
        )

    # Model summary
    models = load_models()
    if models:
        st.subheader("Trained Models")
        model_info = []
        for stat, model in models.items():
            n_features = getattr(model, "n_features_in_", "?")
            model_info.append({"Stat": stat.upper(), "Features": n_features, "Type": type(model).__name__})
        st.dataframe(pd.DataFrame(model_info), use_container_width=True, hide_index=True)

    # Methodology
    st.subheader("Methodology")
    st.markdown("""
1. **Data pipeline** — 5+ seasons of player game logs from the NBA API into SQLite
2. **Feature engineering** — Rolling windows (L5/L10/L20), hit-rate frequencies, pace/matchup context, rest and travel flags (~125 features)
3. **Model training** — HistGradientBoostingRegressor per stat, temporal train/test split (train 2019-23, test 2024-25)
4. **Probability calibration** — 65% L10-baseline / 35% model blend, P(over) via Gaussian CDF with player rolling std
5. **Backtest** — Held-out 2024-25 season validates calibration at P ≥ 65% and P ≥ 70% tiers
    """)


# ---------------------------------------------------------------------------
# PAGE: BACKTEST RESULTS
# ---------------------------------------------------------------------------
elif page == "Backtest Results":
    st.title("Backtest Results — 2024-25 Season")

    # Run backtests for each calibrated stat
    all_results = {}
    for stat in CALIBRATED_STATS:
        bt = run_backtest(stat, threshold=0.55)
        if len(bt) > 0:
            all_results[stat] = bt

    if not all_results:
        st.warning("No backtest results available. Make sure models are trained and features.parquet exists.")
    else:
        # Summary table at key thresholds
        st.subheader("Hit Rates by Confidence Threshold")

        summary_rows = []
        for threshold in [0.55, 0.60, 0.65, 0.70]:
            for stat, bt in all_results.items():
                picks = bt[bt["prob_over"] >= threshold]
                if len(picks) >= 5:  # Minimum sample size
                    hit_rate = picks["hit_over"].mean()
                    summary_rows.append({
                        "Threshold": f"P ≥ {threshold:.0%}",
                        "Stat": stat.upper(),
                        "Picks": len(picks),
                        "Hit Rate": f"{hit_rate:.1%}",
                        "Hit Rate (num)": hit_rate,
                    })

        if summary_rows:
            summary_df = pd.DataFrame(summary_rows)

            # Color-code: green if hit rate >= threshold, red if below
            st.dataframe(
                summary_df[["Threshold", "Stat", "Picks", "Hit Rate"]],
                use_container_width=True,
                hide_index=True,
            )

        # Bar chart of hit rates at P >= 65%
        st.subheader("Hit Rates at P ≥ 65%")
        chart_data = [r for r in summary_rows if r["Threshold"] == "P ≥ 65%"]
        if chart_data:
            chart_df = pd.DataFrame(chart_data)
            fig = go.Figure()
            fig.add_trace(go.Bar(
                x=chart_df["Stat"],
                y=chart_df["Hit Rate (num)"],
                text=[f"{v:.1%}" for v in chart_df["Hit Rate (num)"]],
                textposition="outside",
                marker_color=["#2ecc71" if v >= 0.60 else "#e74c3c" for v in chart_df["Hit Rate (num)"]],
            ))
            fig.add_hline(y=0.65, line_dash="dash", line_color="gray",
                          annotation_text="65% target")
            fig.update_layout(
                yaxis_title="Hit Rate",
                yaxis_tickformat=".0%",
                yaxis_range=[0, 1],
                height=400,
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)

        # Sample picks table
        st.subheader("Sample High-Confidence Picks (P ≥ 70%)")
        player_names = load_player_names()
        high_conf = pd.concat(all_results.values())
        high_conf = high_conf[high_conf["prob_over"] >= 0.70].copy()
        if len(high_conf) > 0:
            high_conf["Player"] = high_conf["player_id"].map(player_names).fillna("Unknown")
            high_conf["Result"] = high_conf["hit_over"].map({1: "✅ HIT", 0: "❌ MISS"})
            display_cols = ["Player", "stat", "line", "predicted", "actual", "prob_over", "Result"]
            st.dataframe(
                high_conf[display_cols]
                .rename(columns={
                    "stat": "Stat", "line": "Line", "predicted": "Predicted",
                    "actual": "Actual", "prob_over": "P(Over)", "Result": "Result"
                })
                .sort_values("P(Over)", ascending=False)
                .head(30),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No picks at P ≥ 70% threshold.")


# ---------------------------------------------------------------------------
# PAGE: CALIBRATION
# ---------------------------------------------------------------------------
elif page == "Calibration":
    st.title("Model Calibration")
    st.markdown(
        "A well-calibrated model means: when it predicts 70% probability, "
        "the event actually happens ~70% of the time."
    )

    all_results = {}
    for stat in CALIBRATED_STATS:
        bt = run_backtest(stat, threshold=0.40)
        if len(bt) > 0:
            all_results[stat] = bt

    if not all_results:
        st.warning("No data available for calibration plots.")
    else:
        for stat in CALIBRATED_STATS:
            if stat not in all_results:
                continue

            bt = all_results[stat]
            st.subheader(f"{STAT_LABELS.get(stat, stat.upper())} Calibration")

            # Bin predictions
            bins = [0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 0.95]
            bt["prob_bin"] = pd.cut(bt["prob_over"], bins=bins)
            cal = bt.groupby("prob_bin", observed=True).agg(
                predicted=("prob_over", "mean"),
                actual=("hit_over", "mean"),
                count=("hit_over", "count"),
            ).dropna()

            if len(cal) >= 2:
                fig = go.Figure()

                # Perfect calibration line
                fig.add_trace(go.Scatter(
                    x=[0.4, 0.95], y=[0.4, 0.95],
                    mode="lines", line=dict(dash="dash", color="gray"),
                    name="Perfect",
                ))

                # Actual calibration
                fig.add_trace(go.Scatter(
                    x=cal["predicted"],
                    y=cal["actual"],
                    mode="markers+lines",
                    marker=dict(size=cal["count"].clip(upper=100) / 3 + 5, color="#3498db"),
                    text=[f"n={c}" for c in cal["count"]],
                    name="Actual",
                ))

                fig.update_layout(
                    xaxis_title="Predicted P(Over)",
                    yaxis_title="Actual Hit Rate",
                    xaxis_range=[0.35, 1.0],
                    yaxis_range=[0.35, 1.0],
                    height=400,
                    showlegend=True,
                )
                st.plotly_chart(fig, use_container_width=True)

                # Data table
                cal_display = cal.reset_index()
                cal_display["predicted"] = cal_display["predicted"].map("{:.1%}".format)
                cal_display["actual"] = cal_display["actual"].map("{:.1%}".format)
                cal_display = cal_display.rename(columns={
                    "prob_bin": "Bin", "predicted": "Avg Predicted",
                    "actual": "Avg Actual", "count": "Picks"
                })
                st.dataframe(cal_display, use_container_width=True, hide_index=True)
            else:
                st.info(f"Not enough data points for {stat.upper()} calibration curve.")


# ---------------------------------------------------------------------------
# PAGE: PLAYER LOOKUP
# ---------------------------------------------------------------------------
elif page == "Player Lookup":
    st.title("Player Lookup")

    df = load_features()
    player_names = load_player_names()
    models = load_models()

    if df is None or not models:
        st.warning("Features or models not loaded.")
    else:
        # Player selector
        test_df = df[df["season"] == "2024-25"].copy()
        if "min_L10" in test_df.columns:
            test_df = test_df[test_df["min_L10"] >= 20]

        available_ids = test_df["player_id"].unique()
        name_to_id = {v: k for k, v in player_names.items() if k in available_ids}
        sorted_names = sorted(name_to_id.keys())

        selected_name = st.selectbox("Select a player", sorted_names)

        if selected_name:
            pid = name_to_id[selected_name]
            player_games = test_df[test_df["player_id"] == pid].sort_values("game_date")

            if len(player_games) == 0:
                st.info("No 2024-25 data for this player.")
            else:
                st.markdown(f"**{selected_name}** — {len(player_games)} games in 2024-25 test set")

                # Show recent stats
                for stat in CALIBRATED_STATS:
                    l10_col = f"{stat}_L10"
                    std_col = f"{stat}_std_L10"
                    last_row = player_games.iloc[-1]

                    l10_val = last_row.get(l10_col, None)
                    std_val = last_row.get(std_col, None)

                    if l10_val is not None and not pd.isna(l10_val):
                        col1, col2 = st.columns(2)
                        col1.metric(
                            f"{STAT_LABELS.get(stat, stat.upper())} L10 Avg",
                            f"{l10_val:.1f}",
                        )
                        if std_val is not None and not pd.isna(std_val):
                            col2.metric(f"{stat.upper()} L10 Std", f"{std_val:.1f}")

                # Game log chart
                st.subheader("Season Trend")
                stat_choice = st.selectbox("Stat to chart", CALIBRATED_STATS,
                                           format_func=lambda x: STAT_LABELS.get(x, x.upper()))

                if stat_choice in player_games.columns:
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(
                        x=player_games["game_date"],
                        y=player_games[stat_choice],
                        mode="markers+lines",
                        name="Actual",
                        marker=dict(size=5),
                    ))

                    l10_col = f"{stat_choice}_L10"
                    if l10_col in player_games.columns:
                        fig.add_trace(go.Scatter(
                            x=player_games["game_date"],
                            y=player_games[l10_col],
                            mode="lines",
                            name="L10 Average",
                            line=dict(dash="dash", color="orange"),
                        ))

                    fig.update_layout(
                        yaxis_title=STAT_LABELS.get(stat_choice, stat_choice.upper()),
                        height=400,
                        showlegend=True,
                    )
                    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# FOOTER
# ---------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.markdown(
    "Built by [Omed Akbari](https://github.com/omedakbari) · "
    "[GitHub Repo](https://github.com/omedakbari/nba-edge-model)"
)
