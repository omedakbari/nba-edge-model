import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
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
BLEND_WEIGHT = 0.65

STAT_LABELS = {"pts": "Points", "reb": "Rebounds", "ast": "Assists"}
CALIBRATED_STATS = ["pts", "reb", "ast"]


# ---------------------------------------------------------------------------
# DATA LOADING
# ---------------------------------------------------------------------------
@st.cache_data
def load_features():
    if not FEATURES_PATH.exists():
        return None
    return pd.read_parquet(FEATURES_PATH)


@st.cache_resource
def load_models():
    """
    Load model pickles. Each pkl is a dict with at least:
        {'model': estimator, 'feature_cols': [...], ...}
    Prefer *_model_v2.pkl over *_model.pkl when both exist.
    Returns dict[stat_name] -> {'model': estimator, 'feature_cols': list, 'meta': dict}
    """
    models = {}
    if not MODELS_DIR.exists():
        return models

    # Discover available pkl files, prefer v2
    available = {}
    for pkl_file in MODELS_DIR.glob("*.pkl"):
        stem = pkl_file.stem  # e.g. "pts_model_v2" or "pts_model"
        if stem == "calibrators":
            continue
        if "_model_v2" in stem:
            stat = stem.replace("_model_v2", "")
            available[stat] = pkl_file  # v2 wins
        elif "_model" in stem and stem.replace("_model", "") not in available:
            stat = stem.replace("_model", "")
            available[stat] = pkl_file

    for stat, pkl_file in available.items():
        try:
            with open(pkl_file, "rb") as f:
                obj = pickle.load(f)

            if isinstance(obj, dict) and "model" in obj:
                models[stat] = {
                    "model": obj["model"],
                    "feature_cols": obj.get("feature_cols", []),
                    "meta": {
                        k: v for k, v in obj.items()
                        if k not in ("model", "feature_cols", "predictions", "actuals")
                    },
                }
            elif hasattr(obj, "predict"):
                # Bare estimator
                feature_cols = []
                if hasattr(obj, "feature_names_in_"):
                    feature_cols = list(obj.feature_names_in_)
                models[stat] = {
                    "model": obj,
                    "feature_cols": feature_cols,
                    "meta": {},
                }
        except Exception as e:
            st.sidebar.warning(f"Could not load {pkl_file.name}: {e}")

    return models


@st.cache_data
def load_player_names():
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(str(DB_PATH))
    players = pd.read_sql("SELECT player_id, full_name FROM players", conn)
    conn.close()
    return dict(zip(players["player_id"], players["full_name"]))


@st.cache_data
def get_db_summary():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(DB_PATH))
    summary = pd.read_sql(
        "SELECT season, COUNT(*) as games, COUNT(DISTINCT player_id) as players "
        "FROM game_logs GROUP BY season ORDER BY season",
        conn,
    )
    total = pd.read_sql("SELECT COUNT(*) as n FROM game_logs", conn).iloc[0]["n"]
    conn.close()
    return summary, total


@st.cache_data
def get_db_diagnostics():
    """Detailed DB diagnostic info."""
    if not DB_PATH.exists():
        return {"error": "Database not found"}
    conn = sqlite3.connect(str(DB_PATH))
    info = {}
    try:
        info["total_players"] = pd.read_sql(
            "SELECT COUNT(*) as n FROM players", conn
        ).iloc[0]["n"]
        info["players_2024_25"] = pd.read_sql(
            "SELECT COUNT(DISTINCT player_id) as n FROM game_logs WHERE season='2024-25'",
            conn,
        ).iloc[0]["n"]
        info["total_rows"] = pd.read_sql(
            "SELECT COUNT(*) as n FROM game_logs", conn
        ).iloc[0]["n"]

        # LeBron check
        lebron = pd.read_sql(
            "SELECT player_id, full_name FROM players WHERE full_name LIKE '%LeBron%'",
            conn,
        )
        info["lebron_in_players"] = len(lebron) > 0
        if len(lebron) > 0:
            pid = lebron.iloc[0]["player_id"]
            lebron_games = pd.read_sql(
                f"SELECT COUNT(*) as n FROM game_logs WHERE player_id={pid} AND season='2024-25'",
                conn,
            ).iloc[0]["n"]
            info["lebron_2024_25_games"] = lebron_games
        else:
            info["lebron_2024_25_games"] = 0
    except Exception as e:
        info["error"] = str(e)
    conn.close()
    return info


# ---------------------------------------------------------------------------
# BACKTEST ENGINE
# ---------------------------------------------------------------------------
@st.cache_data
def run_backtest(stat, threshold=0.55):
    """
    Run backtest for a single stat on 2024-25 test set.
    Uses L10 average as the prop line (matches edge_finder.py logic).
    """
    df = load_features()
    models = load_models()

    if df is None:
        return pd.DataFrame(), "features.parquet not found"
    if stat not in models:
        return pd.DataFrame(), f"No model found for {stat}"

    bundle = models[stat]
    model = bundle["model"]
    feature_cols = bundle["feature_cols"]

    test = df[df["season"] == "2024-25"].copy()
    if len(test) == 0:
        return pd.DataFrame(), "No 2024-25 data in features"

    # Minutes filter
    if "min_L10" in test.columns:
        test = test[test["min_L10"] >= 20].copy()
    if len(test) == 0:
        return pd.DataFrame(), "No rows after min_L10 >= 20 filter"

    # Use saved feature_cols; drop any missing from the dataframe
    available = [c for c in feature_cols if c in test.columns]
    if len(available) == 0:
        return pd.DataFrame(), f"None of the {len(feature_cols)} trained features found in data"

    X_test = test[available].copy()

    # Drop any non-numeric columns, then fill NaN with per-column median
    X_test = X_test.select_dtypes(include=[np.number])
    available = X_test.columns.tolist()
    col_medians = X_test.median(numeric_only=True)
    X_test = X_test.fillna(col_medians)

    # If model expects exact feature count, check
    n_expected = getattr(model, "n_features_in_", len(available))
    if len(available) != n_expected:
        return pd.DataFrame(), (
            f"Feature mismatch: model expects {n_expected}, got {len(available)}"
        )

    try:
        model_pred = model.predict(X_test)
    except Exception as e:
        return pd.DataFrame(), f"Prediction error: {e}"

    # Blend with L10
    l10_col = f"{stat}_L10"
    if l10_col in test.columns:
        l10_vals = test.loc[X_test.index, l10_col].fillna(test[stat]).values
        blended = BLEND_WEIGHT * l10_vals + (1 - BLEND_WEIGHT) * model_pred
    else:
        blended = model_pred

    actual = test.loc[X_test.index, stat].values

    # Player std
    std_col = f"{stat}_std_L10"
    if std_col in test.columns:
        player_std = test.loc[X_test.index, std_col].fillna(actual.std()).values
    else:
        player_std = np.full(len(X_test), actual.std())
    player_std = np.maximum(player_std, 0.5)

    # Use L10 as the prop line
    if l10_col not in test.columns:
        return pd.DataFrame(), f"No {l10_col} column for line calculation"

    lines = test.loc[X_test.index, l10_col].values
    prob_over = 1 - norm.cdf(lines, loc=blended, scale=player_std)
    hit_over = (actual >= lines).astype(int)

    results = []
    test_aligned = test.loc[X_test.index]
    for i in range(len(test_aligned)):
        if prob_over[i] >= threshold and not np.isnan(lines[i]):
            row = test_aligned.iloc[i]
            results.append({
                "player_id": row.get("player_id", 0),
                "game_date": str(row.get("game_date", "")),
                "predicted": round(float(blended[i]), 1),
                "actual": int(actual[i]),
                "line": round(float(lines[i]), 1),
                "prob_over": round(float(prob_over[i]), 3),
                "hit_over": int(hit_over[i]),
                "stat": stat,
            })

    return pd.DataFrame(results), None


# ---------------------------------------------------------------------------
# PAGE CONFIG
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="NBA Edge Model",
    page_icon="🏀",
    layout="wide",
    initial_sidebar_state="expanded",
)

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

        df_feat = load_features()
        n_features = len(df_feat.columns) if df_feat is not None else "?"
        col3.metric("Feature Columns", str(n_features))

        st.subheader("Data by Season")
        st.dataframe(
            summary.rename(columns={"season": "Season", "games": "Games", "players": "Players"}),
            width="stretch",
            hide_index=True,
        )

    # Model summary
    models = load_models()
    if models:
        st.subheader("Trained Models")
        model_info = []
        for stat, bundle in models.items():
            estimator = bundle["model"]
            n_features = getattr(estimator, "n_features_in_", len(bundle["feature_cols"]))
            meta = bundle["meta"]
            model_info.append({
                "Stat": stat.upper(),
                "Model Type": type(estimator).__name__,
                "Features": n_features,
                "Test MAE": f"{meta.get('test_mae', '?'):.3f}" if isinstance(meta.get('test_mae'), (int, float)) else "?",
                "Baseline MAE": f"{meta.get('baseline_mae', '?'):.3f}" if isinstance(meta.get('baseline_mae'), (int, float)) else "?",
            })
        st.dataframe(pd.DataFrame(model_info), width="stretch", hide_index=True)

    # DB Diagnostics
    st.subheader("Database Diagnostics")
    diag = get_db_diagnostics()
    if "error" in diag:
        st.error(diag["error"])
    else:
        d1, d2 = st.columns(2)
        d1.metric("Players in DB", diag.get("total_players", "?"))
        d2.metric("Players with 2024-25 Data", diag.get("players_2024_25", "?"))

        lebron_status = "Yes" if diag.get("lebron_in_players") else "No"
        lebron_games = diag.get("lebron_2024_25_games", 0)
        st.markdown(
            f"**LeBron James in players table:** {lebron_status} · "
            f"**2024-25 game logs:** {lebron_games}"
        )
        if not diag.get("lebron_in_players"):
            st.info(
                "LeBron is not in your database. The original data pull only grabbed "
                "~100-150 players per season. A broader pull would add more players. "
                "Run: python scripts/pull_season_data.py --backfill"
            )

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

    all_results = {}
    errors = {}
    for stat in CALIBRATED_STATS:
        bt, err = run_backtest(stat, threshold=0.55)
        if err:
            errors[stat] = err
        elif len(bt) > 0:
            all_results[stat] = bt

    if errors:
        for stat, err in errors.items():
            st.warning(f"**{stat.upper()}:** {err}")

    if not all_results:
        st.warning(
            "No backtest results available. Ensure models are trained and "
            "features.parquet exists with 2024-25 data."
        )
    else:
        # Summary table
        st.subheader("Hit Rates by Confidence Threshold")
        summary_rows = []
        for threshold in [0.55, 0.60, 0.65, 0.70]:
            for stat, bt in all_results.items():
                picks = bt[bt["prob_over"] >= threshold]
                if len(picks) >= 5:
                    hit_rate = picks["hit_over"].mean()
                    summary_rows.append({
                        "Threshold": f"P ≥ {threshold:.0%}",
                        "Stat": stat.upper(),
                        "Picks": len(picks),
                        "Hit Rate": f"{hit_rate:.1%}",
                        "Hit Rate (num)": hit_rate,
                    })

        if summary_rows:
            st.dataframe(
                pd.DataFrame(summary_rows)[["Threshold", "Stat", "Picks", "Hit Rate"]],
                width="stretch",
                hide_index=True,
            )

        # Bar chart at P >= 65%
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
                marker_color=[
                    "#2ecc71" if v >= 0.60 else "#e74c3c"
                    for v in chart_df["Hit Rate (num)"]
                ],
            ))
            fig.add_hline(
                y=0.65, line_dash="dash", line_color="gray",
                annotation_text="65% target",
            )
            fig.update_layout(
                yaxis_title="Hit Rate",
                yaxis_tickformat=".0%",
                yaxis_range=[0, 1],
                height=400,
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)

        # Sample picks
        st.subheader("Sample High-Confidence Picks (P ≥ 70%)")
        player_names = load_player_names()
        all_bt = pd.concat(all_results.values(), ignore_index=True)
        high_conf = all_bt[all_bt["prob_over"] >= 0.70].copy()
        if len(high_conf) > 0:
            high_conf["Player"] = high_conf["player_id"].map(player_names).fillna("Unknown")
            high_conf["Result"] = high_conf["hit_over"].map({1: "✅ HIT", 0: "❌ MISS"})
            display_cols = ["Player", "stat", "line", "predicted", "actual", "prob_over", "Result"]
            st.dataframe(
                high_conf[display_cols]
                .rename(columns={
                    "stat": "Stat", "line": "Line", "predicted": "Predicted",
                    "actual": "Actual", "prob_over": "P(Over)",
                })
                .sort_values("P(Over)", ascending=False)
                .head(30),
                width="stretch",
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
        bt, err = run_backtest(stat, threshold=0.40)
        if err:
            st.warning(f"**{stat.upper()}:** {err}")
        elif len(bt) > 0:
            all_results[stat] = bt

    if not all_results:
        st.warning("No data available for calibration plots.")
    else:
        for stat in CALIBRATED_STATS:
            if stat not in all_results:
                continue

            bt = all_results[stat]
            st.subheader(f"{STAT_LABELS.get(stat, stat.upper())} Calibration")

            bins = [0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 0.95]
            bt["prob_bin"] = pd.cut(bt["prob_over"], bins=bins)
            cal = bt.groupby("prob_bin", observed=True).agg(
                predicted=("prob_over", "mean"),
                actual=("hit_over", "mean"),
                count=("hit_over", "count"),
            ).dropna()

            if len(cal) >= 2:
                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=[0.4, 0.95], y=[0.4, 0.95],
                    mode="lines", line=dict(dash="dash", color="gray"),
                    name="Perfect",
                ))
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

                cal_display = cal.reset_index()
                cal_display["predicted"] = cal_display["predicted"].map("{:.1%}".format)
                cal_display["actual"] = cal_display["actual"].map("{:.1%}".format)
                cal_display = cal_display.rename(columns={
                    "prob_bin": "Bin", "predicted": "Avg Predicted",
                    "actual": "Avg Actual", "count": "Picks",
                })
                st.dataframe(cal_display, width="stretch", hide_index=True)
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

    if df is None:
        st.warning("features.parquet not found. Run: `python -m src.features.pipeline`")
    elif not models:
        st.warning("No models found. Run: `python -m src.models.player_props`")
    else:
        # Build selector from ALL players in the DB, not just filtered test set
        all_name_to_id = {v: k for k, v in player_names.items()}
        sorted_names = sorted(all_name_to_id.keys())

        if not sorted_names:
            st.warning("No players found in the database.")
        else:
            selected_name = st.selectbox(
                "Search for a player",
                sorted_names,
                index=None,
                placeholder="Type a player name...",
            )

            if selected_name:
                pid = all_name_to_id[selected_name]

                # Check what data exists for this player
                all_player_rows = df[df["player_id"] == pid]
                test_rows = all_player_rows[all_player_rows["season"] == "2024-25"]

                if len(all_player_rows) == 0:
                    st.warning(
                        f"**{selected_name}** exists in the players table but has no rows "
                        f"in features.parquet. They may not have been included in the data pull."
                    )
                elif len(test_rows) == 0:
                    seasons = all_player_rows["season"].unique().tolist()
                    st.info(
                        f"**{selected_name}** has data in seasons: {', '.join(sorted(seasons))} "
                        f"but no 2024-25 rows."
                    )
                else:
                    # Check minutes filter
                    eligible = test_rows.copy()
                    if "min_L10" in eligible.columns:
                        eligible = eligible[eligible["min_L10"] >= 20]

                    if len(eligible) == 0:
                        avg_min = test_rows["min_L10"].mean() if "min_L10" in test_rows.columns else "?"
                        st.info(
                            f"**{selected_name}** has {len(test_rows)} games in 2024-25 "
                            f"but none pass the min_L10 ≥ 20 filter (avg min_L10: {avg_min:.1f}). "
                            f"This player likely has a bench/limited role."
                        )
                    else:
                        player_games = eligible.sort_values("game_date")
                        st.markdown(
                            f"**{selected_name}** — {len(player_games)} eligible games "
                            f"in 2024-25 test set"
                        )

                        # Recent stats
                        last_row = player_games.iloc[-1]
                        cols = st.columns(len(CALIBRATED_STATS))
                        for i, stat in enumerate(CALIBRATED_STATS):
                            l10_col = f"{stat}_L10"
                            std_col = f"{stat}_std_L10"
                            l10_val = last_row.get(l10_col, None)
                            std_val = last_row.get(std_col, None)

                            with cols[i]:
                                if l10_val is not None and not pd.isna(l10_val):
                                    st.metric(
                                        f"{STAT_LABELS.get(stat, stat.upper())} L10",
                                        f"{l10_val:.1f}",
                                    )
                                    if std_val is not None and not pd.isna(std_val):
                                        st.caption(f"Std: {std_val:.1f}")

                        # Season trend chart
                        st.subheader("Season Trend")
                        stat_choice = st.selectbox(
                            "Stat to chart",
                            CALIBRATED_STATS,
                            format_func=lambda x: STAT_LABELS.get(x, x.upper()),
                        )

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
