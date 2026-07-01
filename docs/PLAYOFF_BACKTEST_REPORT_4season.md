# Playoff Backtest Report

**Seasons:** 2021-22, 2022-23, 2023-24, 2024-25  
**Total eligible player-games:** 19,445  
**Line proxy:** Synthetic market line (L10 × stat ratio)  
**Prediction:** 65% L10 + 35% model (with per-stat elevation)  
**Defensive tier:** Live — `team_stats` populated with regular-season DRTG for 2021-22 through 2025-26 (150 rows, 5 seasons × 30 teams). Top-10/bot-10 DRTG classification active.  


---

## 1. Hit Rates per Stat (synthetic market line)

*Line = L10 × playoff market ratio (PTS 0.909, REB 0.986, AST 0.769, PRA 0.939, REB_AST 0.899)*

| Stat | P≥55% (N, HR) | P≥60% (N, HR) | P≥65% (N, HR) | P≥70% (N, HR) | P≥75% (N, HR) | P≥80% (N, HR) |
| --- | --- | --- | --- | --- | --- | --- |
| PTS | 925, 56.9% | 715, 57.8% | 360, 60.8% | 179, 74.3% | 63, 71.4% | 32, 71.9% |
| REB | 1502, 57.7% | 1109, 58.6% | 636, 59.0% | 171, 60.2% | 72, 55.6% | 35, 54.3% |
| AST | 1171, 61.4% | 669, 65.5% | 396, 66.4% | 168, 70.2% | 65, 69.2% | — |
| PRA | 1224, 59.8% | 832, 59.0% | 494, 64.6% | 292, 66.1% | 142, 59.9% | 49, 67.3% |
| REB_AST | 1727, 59.1% | 1179, 59.6% | 725, 61.8% | 549, 64.1% | 352, 67.9% | 142, 65.5% |

---

## 2. Calibration by Decile (Predicted vs Actual)

*Does higher model confidence correspond to higher actual hit rate?*


### PTS (n=3,889)

| Decile | N | Pred P(over) | Actual HR | Error |
| --- | --- | --- | --- | --- |
| 0 | 391 | 30.3% | 31.2% | +0.9% |
| 1 | 391 | 37.0% | 36.8% | -0.2% |
| 2 | 395 | 41.8% | 38.2% | -3.6% |
| 3 | 389 | 46.6% | 47.3% | +0.7% |
| 4 | 382 | 48.6% | 22.8% | -25.8% |
| 5 | 399 | 50.1% | 38.8% | -11.2% |
| 6 | 380 | 52.0% | 45.3% | -6.7% |
| 7 | 384 | 54.8% | 57.8% | +3.0% |
| 8 | 393 | 61.9% | 55.0% | -7.0% |
| 9 | 385 | 70.4% | 60.0% | -10.4% |

### REB (n=3,889)

| Decile | N | Pred P(over) | Actual HR | Error |
| --- | --- | --- | --- | --- |
| 0 | 399 | 29.6% | 24.8% | -4.8% |
| 1 | 389 | 39.1% | 37.8% | -1.3% |
| 2 | 380 | 46.1% | 37.6% | -8.5% |
| 3 | 394 | 48.0% | 24.9% | -23.1% |
| 4 | 384 | 50.1% | 50.5% | +0.4% |
| 5 | 390 | 51.8% | 52.8% | +1.0% |
| 6 | 398 | 56.9% | 54.3% | -2.6% |
| 7 | 387 | 61.2% | 61.5% | +0.3% |
| 8 | 387 | 65.4% | 57.4% | -8.1% |
| 9 | 381 | 71.9% | 56.4% | -15.5% |

### AST (n=3,889)

| Decile | N | Pred P(over) | Actual HR | Error |
| --- | --- | --- | --- | --- |
| 0 | 394 | 37.8% | 41.4% | +3.5% |
| 1 | 384 | 42.2% | 37.2% | -4.9% |
| 2 | 396 | 45.4% | 49.2% | +3.9% |
| 3 | 382 | 48.3% | 41.9% | -6.4% |
| 4 | 405 | 50.0% | 29.4% | -20.7% |
| 5 | 394 | 51.3% | 41.1% | -10.2% |
| 6 | 385 | 53.9% | 54.3% | +0.4% |
| 7 | 378 | 57.2% | 55.8% | -1.4% |
| 8 | 390 | 61.4% | 60.5% | -0.9% |
| 9 | 381 | 70.3% | 67.2% | -3.1% |

### PRA (n=3,889)

| Decile | N | Pred P(over) | Actual HR | Error |
| --- | --- | --- | --- | --- |
| 0 | 398 | 23.0% | 25.1% | +2.1% |
| 1 | 398 | 33.1% | 36.2% | +3.1% |
| 2 | 378 | 40.6% | 35.4% | -5.2% |
| 3 | 383 | 44.1% | 29.2% | -14.9% |
| 4 | 389 | 46.2% | 34.7% | -11.5% |
| 5 | 402 | 52.4% | 44.8% | -7.6% |
| 6 | 378 | 54.0% | 54.2% | +0.2% |
| 7 | 401 | 58.2% | 59.1% | +0.9% |
| 8 | 376 | 63.6% | 53.2% | -10.4% |
| 9 | 386 | 74.5% | 66.8% | -7.7% |

### REB_AST (n=3,889)

| Decile | N | Pred P(over) | Actual HR | Error |
| --- | --- | --- | --- | --- |
| 0 | 398 | 32.6% | 25.4% | -7.3% |
| 1 | 381 | 42.4% | 39.9% | -2.5% |
| 2 | 408 | 48.5% | 48.3% | -0.2% |
| 3 | 376 | 51.9% | 26.1% | -25.9% |
| 4 | 396 | 53.2% | 44.2% | -9.0% |
| 5 | 376 | 54.9% | 54.5% | -0.4% |
| 6 | 387 | 57.1% | 60.7% | +3.6% |
| 7 | 404 | 61.4% | 56.4% | -4.9% |
| 8 | 382 | 69.2% | 54.7% | -14.5% |
| 9 | 381 | 80.5% | 67.7% | -12.8% |

---

## 3. Slice Analysis


### 3a. By Round (P≥55%)

| Slice | PTS HR (N) | REB HR (N) | AST HR (N) | PRA HR (N) | REB_AST HR (N) |
| --- | --- | --- | --- | --- | --- |
| R1 | 59.7% (449) | 58.4% (695) | 62.6% (570) | 61.2% (613) | 58.4% (868) |
| CSF | 52.1% (267) | 57.1% (476) | 60.2% (342) | 59.2% (358) | 62.0% (497) |
| CF | 57.7% (137) | 58.1% (203) | 63.2% (163) | 57.8% (161) | 57.0% (235) |
| Finals | 55.6% (72) | 54.7% (128) | 55.2% (96) | 56.5% (92) | 56.7% (127) |

### 3b. By Game Number in Series (P≥55%)

| Slice | PTS HR (N) | REB HR (N) | AST HR (N) | PRA HR (N) | REB_AST HR (N) |
| --- | --- | --- | --- | --- | --- |
| G1 | 58.8% (165) | 56.7% (270) | 58.5% (212) | 62.8% (218) | 60.2% (314) |
| G2 | 52.7% (165) | 52.6% (268) | 62.1% (211) | 57.8% (218) | 53.5% (310) |
| G3 | 54.9% (162) | 59.2% (267) | 65.2% (210) | 60.7% (214) | 55.8% (308) |
| G4 | 59.9% (162) | 55.7% (264) | 63.3% (207) | 60.0% (215) | 59.9% (307) |
| G5 | 60.8% (143) | 64.8% (227) | 61.4% (171) | 61.6% (185) | 65.9% (258) |
| G6 | 60.0% (90) | 58.7% (143) | 58.2% (110) | 57.7% (123) | 59.9% (162) |
| G7 | 39.5% (38) | 57.1% (63) | 54.0% (50) | 49.0% (51) | 63.2% (68) |

### 3c. Star vs Non-Star (P≥55%)

| Slice | PTS HR (N) | REB HR (N) | AST HR (N) | PRA HR (N) | REB_AST HR (N) |
| --- | --- | --- | --- | --- | --- |
| Star (≥32 min) | 55.2% (513) | 55.5% (661) | 60.3% (713) | 58.5% (641) | 62.7% (747) |
| Non-star | 59.0% (412) | 59.3% (841) | 63.1% (458) | 61.2% (583) | 56.4% (980) |

### 3d. Opponent Defensive Tier (P≥55%)

| Slice | PTS HR (N) | REB HR (N) | AST HR (N) | PRA HR (N) | REB_AST HR (N) |
| --- | --- | --- | --- | --- | --- |
| Top-10 Def | 56.6% (505) | 55.8% (825) | 61.7% (655) | 60.2% (659) | 58.8% (988) |
| Mid Def | 58.7% (298) | 59.9% (569) | 61.6% (401) | 60.4% (422) | 60.3% (577) |
| Bot-10 Def | 53.3% (122) | 60.2% (108) | 59.1% (115) | 55.9% (143) | 56.8% (162) |

### 3e. Rotation Tightening (P≥55%)

| Slice | PTS HR (N) | REB HR (N) | AST HR (N) | PRA HR (N) | REB_AST HR (N) |
| --- | --- | --- | --- | --- | --- |
| Rotation Tightened | 62.5% (320) | 63.3% (520) | 65.7% (405) | 66.5% (445) | 66.2% (580) |
| Normal Rotation | 53.9% (605) | 54.7% (982) | 59.1% (766) | 56.0% (779) | 55.5% (1147) |

---

## 4. Residual Analysis (pred_blended - actual)

*Positive = model over-predicts. Negative = model under-predicts.*

| Stat | Mean Error | Median Error | MAE | N |
| --- | --- | --- | --- | --- |
| PTS | +0.40 | +1.04 | 5.68 | 3889 |
| REB | +0.19 | +0.38 | 2.24 | 3889 |
| AST | -0.08 | +0.18 | 1.54 | 3889 |
| PRA | +1.05 | +1.28 | 7.27 | 3889 |
| REB_AST | +0.35 | +0.65 | 3.10 | 3889 |

### 4a. Residuals by Round

| Round | PTS MeanErr | REB MeanErr | AST MeanErr | PRA MeanErr | REB_AST MeanErr |
| --- | --- | --- | --- | --- | --- |
| R1 | +0.33 | +0.18 | -0.14 | +0.88 | +0.26 |
| CSF | +0.48 | +0.14 | -0.02 | +1.14 | +0.36 |
| CF | +0.18 | +0.30 | -0.15 | +0.96 | +0.40 |
| Finals | +0.92 | +0.36 | +0.27 | +2.18 | +0.91 |

### 4b. Residuals by Star Status

| Tier | PTS MeanErr | REB MeanErr | AST MeanErr | PRA MeanErr | REB_AST MeanErr |
| --- | --- | --- | --- | --- | --- |
| Star (≥32 min) | +0.05 | +0.00 | -0.10 | +0.90 | +0.03 |
| Non-star | +0.60 | +0.30 | -0.06 | +1.14 | +0.54 |

### 4c. Residuals by Game Number

| Game | PTS MeanErr | REB MeanErr | AST MeanErr | PRA MeanErr | REB_AST MeanErr |
| --- | --- | --- | --- | --- | --- |
| G1 | +0.28 | +0.09 | -0.08 | +0.84 | +0.25 |
| G2 | +0.23 | +0.36 | -0.08 | +1.06 | +0.52 |
| G3 | +0.52 | +0.34 | -0.09 | +1.29 | +0.48 |
| G4 | +0.10 | +0.21 | -0.05 | +0.80 | +0.40 |
| G5+ | +0.68 | +0.05 | -0.09 | +1.20 | +0.20 |

---

## 5. Blend Comparison: L10 vs Model vs 65/35 Blend

| Stat | L10 MAE | Model MAE | Blend MAE | Best | N |
| --- | --- | --- | --- | --- | --- |

### 5a. Hit Rate Comparison at P≥55% (by prediction source)

*Each source's probability is computed independently against synthetic line.*

| Stat | Blend HR (N) | L10 HR (N) | Blend Δ |
| --- | --- | --- | --- |
| PTS | 56.9% (925) | 44.3% (3551) | +12.6% |
| REB | — | — | — |
| AST | 61.4% (1171) | 47.7% (3871) | +13.7% |
| PRA | 59.8% (1224) | 46.9% (3199) | +12.9% |
| REB_AST | 59.1% (1727) | 48.6% (3791) | +10.5% |

---

## 6. Executive Summary

### Degradation by stat (pred_blended vs actual, median residual)

| Stat | Median Residual | Over L10% | Assessment |
| --- | --- | --- | --- |
| PTS | +1.04 | 43.3% | ⚠️ Under-predicts (lower line or avoid) |
| REB | +0.38 | 45.7% | Neutral |
| AST | +0.18 | 46.7% | Neutral |
| PRA | +1.28 | 43.8% | ⚠️ Under-predicts (lower line or avoid) |
| REB_AST | +0.65 | 47.8% | Neutral |

### Overall at P≥55% and P≥60%

| Threshold | N Picks | Hit Rate |
| --- | --- | --- |
| P≥55% | 6549 | 59.0% |
| P≥60% | 4504 | 59.8% |
| P≥65% | 2611 | 62.2% |

### Highest-leverage slices (most degraded at P≥55%)

- **PTS in CSF**: hit rate 52.1% (n=267), mean error +1.71
- **REB in Finals**: hit rate 54.7% (n=128), mean error +0.10
- **AST in Finals**: hit rate 55.2% (n=96), mean error +0.21
- **PTS in Finals**: hit rate 55.6% (n=72), mean error +0.55
- **PRA in Finals**: hit rate 56.5% (n=92), mean error +2.01