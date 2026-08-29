# stock_view

An interactive local dashboard over the stocks scan pipeline's output.

```
universe_screen.py -> stock_evaluator.py --batch -> sentiment
                   -> rmt_cluster.py -> position_sizer.py -> scan_report.py
                   -> stock_view  (you are here)
```

**It never re-runs the pipeline.** Not on launch, not on refresh, not from any
button. It reads the JSON files the pipeline already wrote and works entirely
offline. When the data looks stale it says so and tells you to run
`scan_report.py` yourself — re-running the scan means hundreds of network
requests and a rate-limited sentiment stage, which is not something a dashboard
should be able to start by accident.

There is exactly one network call in the whole app: the price sparkline on the
drill-down's **Price history** tab, which only fires when you press its button.

This is a separate app from Dionysus. It does not touch the PyQt6 dashboard.

## Where it goes

`stock_view/` sits beside the pipeline scripts so it can import them as sibling
modules:

```
C:\Users\joey\stocks\
    market_data.py
    stock_evaluator.py
    rmt_cluster.py
    position_sizer.py
    scan_report.py
    data\                  <- the JSON this dashboard reads
    stock_view\            <- this folder
        stock_view.py
        launch.bat
        requirements.txt
        sv\
```

It finds the pipeline using the same `_add_script_dir_to_path()` search the
other scripts use, widened by one level because it sits in a subfolder: it looks
at `$STOCKS_DIR`, then its own folder, then the folder above it, then a `stocks\`
folder beside either. If you keep it somewhere else entirely, set `STOCKS_DIR`
to the folder holding `market_data.py` before launching.

Data comes from `$STOCKS_DATA_DIR`, else `market_data.BASE_DIR \ "data"` — the
same convention as the rest of the project. The resolved path is printed in the
sidebar.

## Launch

Double-click **`launch.bat`**, or:

```
pip install -r requirements.txt
streamlit run stock_view.py
```

Opening it before a scan has ever run is fine — it shows what is missing and
what to run, rather than an error.

## The views

| View | What it shows |
|---|---|
| **Overview** | One row per candidate — composite, rating, liquidity/trend/divergence/conviction flags, sentiment, held marker. Sortable on any column; filter by sector, minimum composite, cluster resolution, and held vs. not held. |
| **Cluster explorer** | The clusters from `clustered.json`, which is richer than the trimmed `cluster` field on each candidate: members, average correlation, dispersion, eigenvalue, share of variance, the resolution and why it resolved that way, plus the winner and its demoted peers. Each cluster gets a correlation heatmap. |
| **Position sizing simulator** | The core view. Move the correlation threshold, reduction factor, basis (raw/cleaned) and reduction mode (proportional/flat), and every not-yet-held candidate is re-sized live. Select a handful of names for a rollup: combined allocation, sector concentration, and which holdings are taking the most size out of the selection. |
| **Ticker drill-down** | The full blob behind one name — every metric group, Piotroski, Altman Z, Graham, Magic Formula, the DCF scenarios, the value screen, insider activity, warnings and notes. Plus the optional price sparkline. |
| **Holdings** | `holdings.json` as a table, with book value totalled **per currency**, and the exit review `position_sizer.py` runs over each holding. Read-only. |

## How the live re-sizing works

`sized_candidates.json` already stores, per candidate, both the raw and the
MP-cleaned correlation to every holding it was compared against:

```json
"sizing": { "correlations": { "RY.TO": { "raw": 0.79, "cleaned": 0.71 } } }
```

The expensive part — fetching prices, building one correlation matrix over
candidates and holdings together, filtering it through Marchenko-Pastur — is
already done and on disk. Moving a slider is arithmetic over data already in
memory, so no refetch and no re-run is needed.

The arithmetic is not reimplemented here. `sizing_scale()` and
`apply_reduction()` are imported from `position_sizer.py`, and
`position_guidance()` from `stock_evaluator.py`, so the simulator cannot drift
from what the pipeline actually does.

One detail worth knowing, because a threshold-and-reduction sketch misses it:
`position_sizer.size_candidate()` applies **two** independent modifiers and
multiplies them.

```
scale = correlation_scale x conviction_scale
```

The correlation modifier asks "do I already own this trade" — that is what the
sliders move. The conviction modifier asks "do the financials and the public
story agree about why the price is where it is" — it comes from the sentiment
stage, not from any slider, and is read back off the stored sizing block
unchanged. Ignoring it would show every reduced candidate as larger than
`position_sizer.py` sized it. The simulator's sidebar has an *Apply the
conviction modifier* tick-box, which is `position_sizer.py --no-conviction`: the
verdict is still reported, it just stops changing size.

The simulator carries an **integrity check** that proves the claim: it re-sizes
every candidate at the parameters the file was written with and compares against
what the file says. If a future change to `position_sizer.py` makes the two
disagree, that check goes red instead of the dashboard quietly showing numbers
the pipeline would not.

### What the heatmap can and cannot show

`position_sizer.py` builds one correlation matrix over candidates *and* holdings
together, but only writes out the candidate-to-holding part of it —
`sizing.correlations` is keyed by holding. The candidate-to-candidate block is
never persisted, and `clustered.json` keeps only each cluster's *average*
pairwise correlation as a single number.

So a members × members heatmap is not available offline, and the dashboard does
not fabricate one. What it draws instead is the pairwise structure that does
exist on disk: each cluster member against each current holding, which is the
relationship the sizing simulator actually acts on.

## What it does not do

- **Never runs** `universe_screen.py`, `stock_evaluator.py`, `rmt_cluster.py`,
  `position_sizer.py` or `scan_report.py`. Run `py scan_report.py` yourself and
  press ↻ Reload.
- **Never writes** to `sized_candidates.json`, `clustered.json` or
  `scored_candidates.json`. Those are pipeline outputs and are treated as
  read-only.
- **Never writes** to `holdings.json` either. It is the one canonical file the
  rest of the pipeline reads, so v1 displays it and no more — edit it directly
  and re-run the scan.
- **Never sums across currencies.** `quote_currency` is CAD or USD per ticker.
  Position guides are percentages of the account and so are comparable across
  both; cost-basis totals are not, so they are reported per currency with the
  mix named. A selection spanning currencies says so.

## Files

```
stock_view.py            entry point: page setup, sidebar, view router
sv/pipeline.py           finds and imports the pipeline modules
sv/data_loader.py        reads the JSON, freshness, currency inference
sv/sizing.py             the live re-size, over position_sizer's own functions
sv/display.py            colour thresholds and flags, matched to scan_report.py
sv/views/overview.py     the candidate table
sv/views/clusters.py     the cluster explorer
sv/views/simulator.py    the sizing simulator
sv/views/drilldown.py    the per-ticker detail
sv/views/holdings.py     holdings and the exit review
```

Scores are coloured on `scan_report.colour()`'s own thresholds — green ≥ 7.5,
yellow ≥ 5.0, red below — and the flag column uses `flag_cells()`'s vocabulary,
so a name reads the same here as it does in the terminal report.

⚠ For informational use only. Not financial advice.
