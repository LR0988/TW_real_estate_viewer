import pandas as pd
import numpy as np
from typing import Dict, Any


def compute_market_heat_score(
    tx_count: int,
    yoy_tx_change: float,
    qoq_tx_change: float,
    yoy_price_change: float,
    qoq_price_change: float,
    avg_unit_price: float,
) -> tuple[float, str]:
    """
    Computes a composite Taiwan Real Estate Market Heat Score (0 - 100)
    and categorical Heat Level based on:
    1. Volume Momentum (35%): YoY transaction growth & QoQ growth
    2. Price Momentum (35%): YoY price growth & QoQ growth
    3. Trading Activity & Liquidity (30%): Base volume scale
    """
    # 1. Volume growth score (0 - 100)
    # yoy_tx_change ranges roughly -50% to +50%
    vol_yoy = max(min(yoy_tx_change if pd.notnull(yoy_tx_change) else 0.0, 60.0), -40.0)
    vol_qoq = max(min(qoq_tx_change if pd.notnull(qoq_tx_change) else 0.0, 40.0), -30.0)
    # Map [-40, +60] to [0, 100]
    score_vol = ((vol_yoy + 40.0) / 100.0) * 70.0 + ((vol_qoq + 30.0) / 70.0) * 30.0
    score_vol = max(0.0, min(100.0, score_vol))

    # 2. Price growth score (0 - 100)
    # yoy_price_change ranges roughly -20% to +30%
    price_yoy = max(min(yoy_price_change if pd.notnull(yoy_price_change) else 0.0, 35.0), -15.0)
    price_qoq = max(min(qoq_price_change if pd.notnull(qoq_price_change) else 0.0, 20.0), -10.0)
    score_price = ((price_yoy + 15.0) / 50.0) * 70.0 + ((price_qoq + 10.0) / 30.0) * 30.0
    score_price = max(0.0, min(100.0, score_price))

    # 3. Base Activity / Liquidity score (0 - 100)
    # Log scale based on transaction count
    if tx_count <= 0:
        score_activity = 0.0
    else:
        # 10 tx -> ~30 pts, 100 tx -> ~60 pts, 1000+ tx -> ~90-100 pts
        score_activity = min(100.0, 20.0 * np.log10(max(tx_count, 1)) + 30.0)
    score_activity = max(0.0, min(100.0, score_activity))

    # Composite weighted score
    heat_score = (score_vol * 0.35) + (score_price * 0.35) + (score_activity * 0.30)
    heat_score = round(float(heat_score), 1)

    # Classify Heat Level
    if heat_score >= 75.0:
        heat_level = "爆發熱絡"  # Hot / High demand & price push
    elif heat_score >= 58.0:
        heat_level = "穩健成長"  # Warm / Healthy activity
    elif heat_score >= 42.0:
        heat_level = "盤整持平"  # Neutral / Stable
    else:
        heat_level = "冷卻觀望"  # Cool / Low liquidity

    return heat_score, heat_level
