"""
Pivot Point Calculator for Financial Market Analysis

This module identifies and analyzes significant price levels (pivots) in financial market data
using adaptive kernel density estimation and time-weighted volume analysis.

STATISTICAL FOUNDATION:
The algorithm is based on Student's t-distribution principles for financial time series:
- Heavy-tailed distribution: Financial returns exhibit heavier tails than normal distribution
- Volume-weighted density: Uses trading volume as probability weights
- Adaptive bandwidth: Adjusts kernel width based on data concentration
- Peak detection: Identifies local maxima in the probability density function
- Time decay: Recent data gets exponential weighting (exp(-days/90))

This approach captures the reality that market participants cluster around certain price
levels with varying conviction (volume), and these levels persist longer when
supported by higher trading activity.

SOURCES:
- Input CSV file containing OHLCV (Open, High, Low, Close, Volume) data with datetime column
- Configuration parameters from pivot_config.py (PIVOT_CONFIG)

OUTPUTS:
- CSV file: Daily pivot points with enrichment metrics (density, width, stickiness, rejection rates)
- Parquet file: Same data optimized for ML/downstream consumption
- Console output: Progress updates and summary statistics

FUNCTIONALITY:
1. Adaptive pivot detection using Gaussian KDE with bandwidth optimization
2. Time-weighted volume analysis for recent data emphasis (90-day decay)
3. Pivot enrichment metrics:
   - Density: Volume strength at pivot level
   - Width: Price range around pivot
   - Stickiness: Average duration price stays near pivot
   - Rejection rate: Ratio of price reversals vs breakthroughs
4. Binned features for ML (tertile rankings: 0=bottom 33%, 1=middle 33%, 2=top 33%)
5. Historical archive generation with 3-month rolling window analysis

ALGORITHM:
- Uses scipy.stats.gaussian_kde for volume-weighted price density estimation
- Adaptive bandwidth tuning (0.005-1.0) to achieve 5-8 pivots per side
- scipy.signal.find_peaks for density peak detection
- Time-decay weighting: exp(-days_ago/90) for recency emphasis
- Neighbor-aware pivot boundaries to prevent overlap
"""

import pandas as pd
import os
import sys
import numpy as np
from scipy.stats import gaussian_kde
from scipy.signal import find_peaks, peak_widths

# Add parent directory to path for imports
from .pivot_config import PIVOT_CONFIG

input_csv = PIVOT_CONFIG['input_file']
output_csv = PIVOT_CONFIG.get('output_file', 'data/pivots/ONDS_pivots.csv')
pivots_range = PIVOT_CONFIG['pivots_range']

def calculate_time_weights(index, last_time, decay_days=90):
    """Vectorized exponential decay weights."""
    days_ago = (last_time - index).total_seconds() / 86400
    return np.exp(-days_ago / decay_days)

def add_binned_features(pivots_list):
    """
    Adds {feature}_binned columns to each pivot using within-day tertile ranking.
    0 = bottom 33%, 1 = middle 33%, 2 = top 33%
    """
    if not pivots_list or len(pivots_list) < 2:
        for p in pivots_list:
            p['density_binned'] = 0
            p['width_binned'] = 0
            p['stickiness_binned'] = 0
            p['rejection_rate_binned'] = 0
        return pivots_list

    features = ['density', 'width', 'stickiness', 'rejection_rate']

    for feature in features:
        values = np.array([p[feature] for p in pivots_list])
        t33, t67 = np.percentile(values, [33.33, 66.67])

        for p in pivots_list:
            v = p[feature]
            if v <= t33:
                p[f'{feature}_binned'] = 0
            elif v <= t67:
                p[f'{feature}_binned'] = 1
            else:
                p[f'{feature}_binned'] = 2

    return pivots_list

def enrich_pivots(up_to_date_df, pivots_list, pivots_range=0.10):
    if not pivots_list or up_to_date_df.empty:
        return pivots_list

    last_close = up_to_date_df['close'].iloc[-1]
    last_time = up_to_date_df.index[-1]
    
    mask = (up_to_date_df['close'] >= last_close * (1 - pivots_range)) & \
           (up_to_date_df['close'] <= last_close * (1 + pivots_range))
    relevant_df = up_to_date_df[mask]
    if relevant_df.empty: return pivots_list

    weights = calculate_time_weights(relevant_df.index, last_time)
    closes = relevant_df['close'].values
    highs = relevant_df['high'].values
    lows = relevant_df['low'].values

    pivots_list = sorted(pivots_list, key=lambda x: x['price'])
    num_pivots = len(pivots_list)

    for idx, p_dict in enumerate(pivots_list):
        price = p_dict['price']
        half_w_safe = p_dict['width'] * 0.75
        
        # Neighbor-aware boundaries
        l_dist = (price - pivots_list[idx-1]['price'])/2 if idx > 0 else half_w_safe
        u_dist = (pivots_list[idx+1]['price'] - price)/2 if idx < num_pivots-1 else half_w_safe
        
        lower_bound = price - min(half_w_safe, l_dist)
        upper_bound = price + min(half_w_safe, u_dist)

        # Rejection/Breach based on High/Low (Encounters)
        has_touched = (highs >= lower_bound) & (lows <= upper_bound)
        diff = np.diff(has_touched.astype(int), prepend=0, append=0)
        entries = np.where(diff == 1)[0]
        exits = np.where(diff == -1)[0]

        weighted_duration_sum = 0
        weight_sum = 0
        rejections = 0
        breaches = 0
        
        for start, end in zip(entries, exits):
            # 1. Average Stickiness Logic
            # Duration (m) * Weight of this visit (w)
            visit_weight = weights[start]
            duration = end - start
            
            weighted_duration_sum += (duration * visit_weight)
            weight_sum += visit_weight

            # 2. Rejection Logic
            if start > 0 and end < len(closes):
                price_before = closes[start - 1]
                price_after = closes[end]
                
                entered_from_below = price_before < lower_bound
                left_to_below = price_after < lower_bound
                
                if entered_from_below == left_to_below:
                    rejections += visit_weight
                else:
                    breaches += visit_weight

        # Calculations
        p_dict['stickiness'] = round(weighted_duration_sum / weight_sum, 2) if weight_sum > 0 else 0
        
        total_encounters = rejections + breaches
        p_dict['rejection_rate'] = round(rejections / total_encounters, 3) if total_encounters > 0 else 0

    return pivots_list

def get_adaptive_pivots(up_to_date_df, pivots_range=0.10):
    if up_to_date_df.empty: return []
    
    last_close = up_to_date_df['close'].iloc[-1]
    last_time = up_to_date_df.index[-1] # The most recent timestamp

    lower_limit = last_close * (1 - pivots_range)
    upper_limit = last_close * (1 + pivots_range)
    
    full_mask = (up_to_date_df['close'] >= lower_limit) & (up_to_date_df['close'] <= upper_limit)
    subset = up_to_date_df[full_mask].copy()
    
    if len(subset) < 20: return []
    
    subset['time_weight'] = calculate_time_weights(subset.index, last_time)
    
    # Calculate Weighted Volume
    subset['weighted_volume'] = subset['volume'] * subset['time_weight']

    # Pre-aggregate to speed up KDE
    agg = subset.groupby(subset['close'].round(4))['weighted_volume'].sum().reset_index()
    prices, volumes = agg['close'].values, agg['weighted_volume'].values
    
    bw = 0.1
    lower_peaks, upper_peaks = [], []
    axis = np.linspace(lower_limit, upper_limit, 400)
    
    for _ in range(10):
        try:
            kde = gaussian_kde(prices, weights=volumes, bw_method=bw)
            density = kde(axis)
            
            mid_idx = np.searchsorted(axis, last_close)
            lower_peaks, _ = find_peaks(density[:mid_idx])
            upper_peaks, _ = find_peaks(density[mid_idx:])
            
            l_count, u_count = len(lower_peaks), len(upper_peaks)
            
            if 5 <= l_count <= 8 and 5 <= u_count <= 8:
                break # Success!
            
            # Adjust bandwidth; keep it within a sane range [0.005, 1.0]
            if (l_count + u_count) > 16:
                bw = min(bw * 1.4, 1.0)
            else:
                bw = max(bw * 0.6, 0.005)
        except:
            break
            
    all_peak_indices = np.concatenate([lower_peaks, upper_peaks + mid_idx])
    
    # Calculate peak widths
    if len(all_peak_indices) > 0:
        # This returns: [widths, width_heights, left_ips, right_ips]
        widths_res = peak_widths(density, all_peak_indices, rel_height=0.5)
    
        # The 'widths' are in index units, we convert them to price units
        price_step = axis[1] - axis[0]
        actual_widths = widths_res[0] * price_step
    else:
        actual_widths = []

    # Create a list of dictionaries for each pivot
    pivots_data = []
    for i in range(len(all_peak_indices)):
        current_pivot_price = axis[all_peak_indices[i]]
        current_actual_width = actual_widths[i]
        rel_width = (current_actual_width / current_pivot_price)
        pivots_data.append({
            'price': round(float(current_pivot_price), 4),
            'width': round(float(current_actual_width), 4),
            'rel_width': round(float(rel_width), 6)  ,
            'density': round(float(density[all_peak_indices[i]]), 3) # Peak 'height' (volume strength)
        })
        
    return pivots_data

def save_pivots_archive(all_pivots, base_filename):
    """
    Flattens nested pivots and saves to both CSV and Parquet.
    Ensures all Numpy types are cast to standard Python floats.
    """
    flattened_rows = []
    
    for entry in all_pivots:
        dt = entry['date']
        for p in entry['pivots']:
            # Create a flat row and cast everything to standard float/int
            row = {
                'date': dt,
                'price': float(p['price']),
                'width': float(p['width']),
                'rel_width': float(p['rel_width']),
                'density': float(p['density']),
                'stickiness': float(p['stickiness']),
                'rejection_rate': float(p['rejection_rate']),
                'density_binned': int(p['density_binned']),
                'width_binned': int(p['width_binned']),
                'stickiness_binned': int(p['stickiness_binned']),
                'rejection_rate_binned': int(p['rejection_rate_binned'])
            }
            flattened_rows.append(row)
    
    final_df = pd.DataFrame(flattened_rows)
    
    # 1. Save parquet (For ML/Downstream consumption)
    parquet_path = base_filename if base_filename.endswith('.parquet') else f"{base_filename}.parquet"
    final_df.to_parquet(parquet_path, index=False)
    
    # 2. Save csv (For human inspection)
    csv_path = base_filename if base_filename.endswith('.csv') else f"{base_filename}.csv"
    final_df.to_csv(csv_path, index=False)
    
    print(f"\n--- Archive Summary ---")
    print(f"Total Pivots Archived: {len(final_df)}")
    print(f"CSV saved to: {csv_path}")
    print(f"Parquet saved to: {parquet_path}")

def main(file_path=input_csv):
    if not os.path.exists(file_path):
        print(f"Error: The file '{file_path}' was not found.")
        return

    training_df = pd.read_csv(file_path, parse_dates=['datetime'])
    training_df = training_df.sort_values('datetime').set_index('datetime')

    # Floor to 'D' is the fastest way to get unique calendar dates
    unique_dates = training_df.index.floor('D').unique()

    all_pivots = []
    
    for i, current_date in enumerate(unique_dates):
        # 3-month lookback
        start_date = current_date - pd.DateOffset(months=3)
        up_to_date_df = training_df.loc[start_date : current_date - pd.Timedelta(nanoseconds=1)]
        
        if not up_to_date_df.empty:
            pivots = get_adaptive_pivots(up_to_date_df, pivots_range)
            enriched_pivots = enrich_pivots(up_to_date_df, pivots, pivots_range)
            enriched_pivots = add_binned_features(enriched_pivots) 
            
            if enriched_pivots:
                all_pivots.append({'date': current_date.date(), 'pivots': enriched_pivots})
            
            if (i + 1) % 25 == 0 or i == len(unique_dates) - 1:
                print(f"--- Iteration {i+1}: {current_date.date()} ---")
                print(f"Pivots found: {len(enriched_pivots)} (Top Price: {enriched_pivots[0]['price'] if enriched_pivots else 'N/A'}; Width: {enriched_pivots[0]['width'] if enriched_pivots else 'N/A'}; Stickiness: {enriched_pivots[0]['stickiness'] if enriched_pivots else 'N/A'}; Rejection rate: {enriched_pivots[0]['rejection_rate'] if enriched_pivots else 'N/A'})")


    # Final Export
    if all_pivots:
        save_pivots_archive(all_pivots, output_csv)

if __name__ == "__main__":
    main()
    input("\nPress Enter to exit...")