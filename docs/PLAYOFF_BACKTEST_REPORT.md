# Playoff Backtest Report

**Seasons:** 2024-25  
**Total eligible player-games:** 5,805  
**Line proxy:** Synthetic market line (L10 × stat ratio)  
**Prediction:** 65% L10 + 35% model (with per-stat elevation)  
**Defensive tier:** ⚠️ PROVISIONAL — all games classified as 'mid' because `team_stats` is empty. Def-tier slice analysis will be flat until DRTG data is populated.  


---

## 1. Hit Rates per Stat (synthetic market line)

*Line = L10 × playoff market ratio (PTS 0.909, REB 0.986, AST 0.769, PRA 0.939, REB_AST 0.899)*

| Stat | P≥55% (N, HR) | P≥60% (N, HR) | P≥65% (N, HR) | P≥70% (N, HR) | P≥75% (N, HR) | P≥80% (N, HR) |
| --- | --- | --- | --- | --- | --- | --- |
| PTS | 239, 49.0% | 169, 49.7% | 117, 53.0% | 45, 64.4% | 16, 68.8% | 12, 66.7% |
| REB | 410, 51.7% | 343, 47.5% | 228, 49.1% | 37, 48.6% | 32, 53.1% | 18, 55.6% |
| AST | 377, 54.1% | 218, 61.5% | 157, 65.0% | 64, 68.8% | 29, 69.0% | — |
| PRA | 333, 53.2% | 244, 51.2% | 158, 48.1% | 139, 52.5% | 75, 44.0% | 11, 45.5% |
| REB_AST | 540, 49.4% | 376, 50.3% | 230, 51.7% | 197, 52.3% | 164, 56.7% | 65, 52.3% |

---

## 2. Calibration by Decile (Predicted vs Actual)

*Does higher model confidence correspond to higher actual hit rate?*


### PTS (n=1,161)

| Decile | N | Pred P(over) | Actual HR | Error |
| --- | --- | --- | --- | --- |
| 0 | 120 | 31.3% | 35.8% | +4.5% |
| 1 | 124 | 37.9% | 42.7% | +4.8% |
| 2 | 105 | 44.1% | 32.4% | -11.8% |
| 3 | 122 | 48.0% | 33.6% | -14.4% |
| 4 | 113 | 48.9% | 23.0% | -25.8% |
| 5 | 113 | 50.5% | 34.5% | -16.0% |
| 6 | 124 | 51.7% | 49.2% | -2.5% |
| 7 | 108 | 53.5% | 46.3% | -7.2% |
| 8 | 138 | 60.6% | 50.0% | -10.6% |
| 9 | 94 | 71.8% | 47.9% | -23.9% |

### REB (n=1,161)

| Decile | N | Pred P(over) | Actual HR | Error |
| --- | --- | --- | --- | --- |
| 0 | 123 | 29.9% | 23.6% | -6.3% |
| 1 | 120 | 41.1% | 43.3% | +2.2% |
| 2 | 109 | 46.9% | 25.7% | -21.2% |
| 3 | 133 | 48.3% | 27.8% | -20.5% |
| 4 | 97 | 50.3% | 46.4% | -3.9% |
| 5 | 119 | 50.8% | 48.7% | -2.1% |
| 6 | 117 | 55.0% | 64.1% | +9.1% |
| 7 | 115 | 62.8% | 44.3% | -18.5% |
| 8 | 127 | 67.3% | 50.4% | -16.9% |
| 9 | 101 | 72.9% | 47.5% | -25.4% |

### AST (n=1,161)

| Decile | N | Pred P(over) | Actual HR | Error |
| --- | --- | --- | --- | --- |
| 0 | 121 | 38.5% | 39.7% | +1.1% |
| 1 | 127 | 44.3% | 33.9% | -10.5% |
| 2 | 101 | 46.8% | 46.5% | -0.3% |
| 3 | 121 | 49.3% | 42.1% | -7.2% |
| 4 | 131 | 50.1% | 32.1% | -18.0% |
| 5 | 98 | 52.1% | 35.7% | -16.3% |
| 6 | 133 | 54.6% | 47.4% | -7.2% |
| 7 | 104 | 58.1% | 38.5% | -19.7% |
| 8 | 115 | 63.5% | 53.0% | -10.5% |
| 9 | 110 | 71.4% | 70.0% | -1.4% |

### PRA (n=1,161)

| Decile | N | Pred P(over) | Actual HR | Error |
| --- | --- | --- | --- | --- |
| 0 | 119 | 24.5% | 33.6% | +9.2% |
| 1 | 133 | 35.3% | 38.3% | +3.0% |
| 2 | 97 | 43.0% | 27.8% | -15.2% |
| 3 | 126 | 44.8% | 37.3% | -7.5% |
| 4 | 107 | 46.4% | 19.6% | -26.8% |
| 5 | 132 | 52.8% | 26.5% | -26.3% |
| 6 | 99 | 53.5% | 60.6% | +7.1% |
| 7 | 124 | 57.4% | 56.5% | -0.9% |
| 8 | 112 | 66.0% | 51.8% | -14.3% |
| 9 | 112 | 77.4% | 50.9% | -26.5% |

### REB_AST (n=1,161)

| Decile | N | Pred P(over) | Actual HR | Error |
| --- | --- | --- | --- | --- |
| 0 | 117 | 35.4% | 19.7% | -15.7% |
| 1 | 127 | 43.7% | 50.4% | +6.7% |
| 2 | 113 | 50.3% | 29.2% | -21.1% |
| 3 | 108 | 52.0% | 28.7% | -23.3% |
| 4 | 123 | 53.8% | 46.3% | -7.4% |
| 5 | 111 | 55.3% | 41.4% | -13.9% |
| 6 | 120 | 58.1% | 56.7% | -1.5% |
| 7 | 112 | 62.5% | 46.4% | -16.1% |
| 8 | 119 | 72.2% | 48.7% | -23.5% |
| 9 | 111 | 82.7% | 55.0% | -27.8% |

---

## 3. Slice Analysis


### 3a. By Round (P≥55%)

| Slice | PTS HR (N) | REB HR (N) | AST HR (N) | PRA HR (N) | REB_AST HR (N) |
| --- | --- | --- | --- | --- | --- |
| R1 | 48.4% (122) | 51.6% (161) | 54.7% (161) | 54.9% (162) | 48.2% (247) |
| CSF | 46.9% (64) | 54.1% (133) | 55.4% (101) | 58.1% (86) | 56.2% (160) |
| CF | 46.2% (39) | 49.3% (67) | 51.5% (66) | 42.0% (50) | 36.9% (84) |
| Finals | — | 49.0% (49) | 53.1% (49) | 48.6% (35) | 55.1% (49) |

### 3b. By Game Number in Series (P≥55%)

| Slice | PTS HR (N) | REB HR (N) | AST HR (N) | PRA HR (N) | REB_AST HR (N) |
| --- | --- | --- | --- | --- | --- |
| G1 | 55.8% (43) | 51.3% (76) | 51.5% (66) | 61.0% (59) | 53.1% (98) |
| G2 | 52.4% (42) | 53.4% (73) | 57.6% (66) | 61.0% (59) | 50.0% (94) |
| G3 | 41.9% (43) | 53.3% (75) | 50.7% (67) | 41.4% (58) | 44.2% (95) |
| G4 | 53.5% (43) | 43.2% (74) | 56.7% (67) | 50.0% (58) | 46.3% (95) |
| G5 | 45.9% (37) | 63.5% (63) | 50.0% (56) | 60.0% (50) | 57.1% (84) |
| G6 | 47.6% (21) | 45.5% (33) | 61.8% (34) | 46.9% (32) | 44.9% (49) |
| G7 | — | 43.8% (16) | 52.4% (21) | 41.2% (17) | 48.0% (25) |

### 3c. Star vs Non-Star (P≥55%)

| Slice | PTS HR (N) | REB HR (N) | AST HR (N) | PRA HR (N) | REB_AST HR (N) |
| --- | --- | --- | --- | --- | --- |
| Star (≥32 min) | 49.5% (188) | 52.4% (185) | 52.4% (212) | 53.3% (214) | 55.8% (233) |
| Non-star | 47.1% (51) | 51.1% (225) | 56.4% (165) | 52.9% (119) | 44.6% (307) |

### 3d. Opponent Defensive Tier (P≥55%)

| Slice | PTS HR (N) | REB HR (N) | AST HR (N) | PRA HR (N) | REB_AST HR (N) |
| --- | --- | --- | --- | --- | --- |
| Top-10 Def | 47.8% (136) | 48.7% (230) | 53.1% (207) | 55.9% (179) | 51.2% (326) |
| Mid Def | 50.7% (75) | 57.2% (159) | 54.7% (128) | 50.9% (112) | 46.4% (179) |
| Bot-10 Def | — | — | 57.1% (42) | 47.6% (42) | 48.6% (35) |

### 3e. Rotation Tightening (P≥55%)

| Slice | PTS HR (N) | REB HR (N) | AST HR (N) | PRA HR (N) | REB_AST HR (N) |
| --- | --- | --- | --- | --- | --- |
| Rotation Tightened | — | 68.6% (35) | — | — | 61.2% (49) |
| Normal Rotation | 48.4% (223) | 50.1% (375) | 53.4% (356) | 52.9% (308) | 48.3% (491) |

---

## 4. Residual Analysis (pred_blended - actual)

*Positive = model over-predicts. Negative = model under-predicts.*

| Stat | Mean Error | Median Error | MAE | N |
| --- | --- | --- | --- | --- |
| PTS | +0.93 | +1.87 | 5.98 | 1161 |
| REB | +0.41 | +0.65 | 2.32 | 1161 |
| AST | +0.19 | +0.46 | 1.55 | 1161 |
| PRA | +2.15 | +2.64 | 7.85 | 1161 |
| REB_AST | +0.88 | +1.22 | 3.25 | 1161 |

### 4a. Residuals by Round

| Round | PTS MeanErr | REB MeanErr | AST MeanErr | PRA MeanErr | REB_AST MeanErr |
| --- | --- | --- | --- | --- | --- |
| R1 | +0.86 | +0.37 | +0.05 | +1.85 | +0.66 |
| CSF | +0.90 | +0.19 | +0.24 | +1.92 | +0.71 |
| CF | +1.12 | +0.99 | +0.33 | +3.17 | +1.63 |
| Finals | +1.10 | +0.48 | +0.62 | +3.12 | +1.56 |

### 4b. Residuals by Star Status

| Tier | PTS MeanErr | REB MeanErr | AST MeanErr | PRA MeanErr | REB_AST MeanErr |
| --- | --- | --- | --- | --- | --- |
| Star (≥32 min) | +1.20 | +0.18 | +0.29 | +2.83 | +0.68 |
| Non-star | +0.79 | +0.53 | +0.13 | +1.81 | +0.98 |

### 4c. Residuals by Game Number

| Game | PTS MeanErr | REB MeanErr | AST MeanErr | PRA MeanErr | REB_AST MeanErr |
| --- | --- | --- | --- | --- | --- |
| G1 | +0.90 | +0.15 | +0.13 | +1.78 | +0.57 |
| G2 | +0.68 | +0.41 | +0.15 | +1.88 | +0.85 |
| G3 | +1.11 | +0.56 | +0.23 | +2.50 | +1.07 |
| G4 | +0.55 | +0.50 | +0.28 | +1.93 | +1.05 |
| G5+ | +1.21 | +0.43 | +0.16 | +2.48 | +0.86 |

---

## 5. Blend Comparison: L10 vs Model vs 65/35 Blend

| Stat | L10 MAE | Model MAE | Blend MAE | Best | N |
| --- | --- | --- | --- | --- | --- |

### 5a. Hit Rate Comparison at P≥55% (by prediction source)

*Each source's probability is computed independently against synthetic line.*

| Stat | Blend HR (N) | L10 HR (N) | Blend Δ |
| --- | --- | --- | --- |
| PTS | 49.0% (239) | 41.3% (1098) | +7.6% |
| REB | — | — | — |
| AST | 54.1% (377) | 43.7% (1161) | +10.4% |
| PRA | 53.2% (333) | 43.4% (968) | +9.8% |
| REB_AST | 49.4% (540) | 44.2% (1112) | +5.3% |

---

## 6. Executive Summary

### Degradation by stat (pred_blended vs actual, median residual)

| Stat | Median Residual | Over L10% | Assessment |
| --- | --- | --- | --- |
| PTS | +1.87 | 39.7% | ⚠️ Under-predicts (lower line or avoid) |
| REB | +0.65 | 41.9% | ⚠️ Under-predicts (lower line or avoid) |
| AST | +0.46 | 43.0% | ⚠️ Under-predicts (lower line or avoid) |
| PRA | +2.64 | 40.1% | ⚠️ Under-predicts (lower line or avoid) |
| REB_AST | +1.22 | 42.5% | ⚠️ Under-predicts (lower line or avoid) |

### Overall at P≥55% and P≥60%

| Threshold | N Picks | Hit Rate |
| --- | --- | --- |
| P≥55% | 1899 | 51.4% |
| P≥60% | 1350 | 51.5% |
| P≥65% | 890 | 52.9% |

### Highest-leverage slices (most degraded at P≥55%)

- **REB_AST in CF**: hit rate 36.9% (n=84), mean error +2.01
- **PRA in CF**: hit rate 42.0% (n=50), mean error +5.60
- **PTS in CF**: hit rate 46.2% (n=39), mean error +2.41
- **PTS in CSF**: hit rate 46.9% (n=64), mean error +2.56
- **REB_AST in R1**: hit rate 48.2% (n=247), mean error +0.97