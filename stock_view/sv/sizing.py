"""Live re-sizing, using position_sizer's own functions.

sized_candidates.json already stores, per candidate, both the raw and the
MP-cleaned correlation to every holding it was compared against. That is the
whole reason the threshold / reduction / basis controls need no network and no
re-run: the expensive part (fetching prices, building one correlation matrix
over candidates and holdings together, filtering it) is already on disk, and
only the arithmetic on top of it changes when a slider moves.

The arithmetic itself is not reimplemented here. sizing_scale() and
apply_reduction() are imported from position_sizer.py, and this module's job is
to feed them the same inputs size_candidate() feeds them, in the same order.

One thing the recompute has to carry that a threshold/reduction sketch misses:
position_sizer applies TWO independent modifiers and multiplies them.

    scale = correlation_scale x conviction_scale

The correlation modifier asks "do I already own this trade" and is what the
sliders move. The conviction modifier asks "do the financials and the public
story agree about why the price is where it is" - it comes from the sentiment
stage, not from any slider, and it is read back off the stored sizing block
unchanged. Dropping it would make every reduced candidate look bigger here than
position_sizer.py sized it, which is exactly the drift this module exists to
avoid. verify() proves the two agree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .pipeline import PIPELINE


def _num(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


@dataclass
class Sized:
    """One candidate re-sized at the current control settings."""
    ticker: str
    name: Optional[str]
    sector: Optional[str]
    composite: Optional[float]
    already_held: bool

    base_guide: Optional[str]
    base_low: Optional[float]
    base_high: Optional[float]

    adjusted_guide: Optional[str]
    adjusted_low: Optional[float]
    adjusted_high: Optional[float]

    scale: float                      # the combined scale actually applied
    correlation_scale: float          # the half the sliders move
    conviction_scale: float           # the half the sentiment stage set
    conviction: Optional[str]

    max_correlation: Optional[float]  # worst correlation on the chosen basis
    correlated_with: Optional[str]    # the holding driving the cut
    correlation_adjusted: bool
    note: str

    @property
    def cut_pct(self) -> float:
        """How much of the base size is gone, as a percentage."""
        return (1.0 - self.scale) * 100.0

    @property
    def correlation_cut_pct(self) -> float:
        return (1.0 - self.correlation_scale) * 100.0

    @property
    def points_given_up(self) -> Optional[float]:
        """Percentage points of account lost off the top of the base range.

        The high end is the number a concentration question is asked about, so
        that is the end this measures.
        """
        if self.base_high is None or self.adjusted_high is None:
            return None
        return self.base_high - self.adjusted_high


@dataclass
class Controls:
    """The simulator's four inputs."""
    threshold: float
    reduction_factor: float
    basis: str                 # "raw" | "cleaned"
    flat: bool                 # flat reduction instead of proportional
    apply_conviction: bool = True

    @property
    def reduction_mode(self) -> str:
        return "flat" if self.flat else "proportional"


def controls_from_params(params: dict) -> Controls:
    """Slider defaults from the file's own params, then position_sizer's.

    The file is the better default: it says what these candidates were actually
    sized at, so the simulator opens showing the numbers already on screen
    elsewhere in the dashboard rather than a different set.
    """
    defaults = PIPELINE.defaults
    threshold = _num(params.get("correlation_threshold"))
    reduction = _num(params.get("reduction_factor"))
    basis = params.get("correlation_basis")
    mode = params.get("reduction_mode")
    return Controls(
        threshold=defaults["correlation_threshold"] if threshold is None else threshold,
        reduction_factor=(defaults["reduction_factor"] if reduction is None
                          else reduction),
        basis=basis if basis in ("raw", "cleaned") else defaults["correlation_basis"],
        flat=(mode == "flat"),
    )


def worst_pair(pairs: dict, basis: str):
    """The holding a candidate is most correlated with, on the chosen basis.

    position_sizer picks with max(..., key=pairs[h][basis]) - the largest signed
    correlation, not the largest magnitude. A strongly negative correlation is
    not a duplicate position, so it should not be the one driving a cut.
    """
    usable = {h: _num(v.get(basis)) for h, v in (pairs or {}).items()
              if isinstance(v, dict) and _num(v.get(basis)) is not None}
    if not usable:
        return None, None
    holding = max(usable, key=lambda h: usable[h])
    return holding, usable[holding]


def resize(candidate: dict, controls: Controls) -> Sized:
    """Re-run one candidate's sizing at the current controls.

    Follows position_sizer.size_candidate()'s order of decisions exactly: a held
    name is not sized as a new position, a name with no stored correlations is
    not adjusted, and only a correlation above the threshold moves anything.
    """
    if not PIPELINE.ok:
        raise RuntimeError("position_sizer.py could not be imported")

    sizing = candidate.get("sizing") or {}
    base_guide = sizing.get("base_guide")
    base_low = _num(sizing.get("base_low_pct"))
    base_high = _num(sizing.get("base_high_pct"))

    conviction_scale = _num(sizing.get("conviction_scale"))
    if conviction_scale is None:
        conviction_scale = 1.0
    if not controls.apply_conviction:
        conviction_scale = 1.0

    correlation_scale, correlation_adjusted = 1.0, False
    worst_holding, worst_corr = None, None
    note = ""

    if candidate.get("already_held"):
        note = "already held - not sized as a new position"
    else:
        pairs = sizing.get("correlations") or {}
        if not pairs:
            note = "no correlation to holdings available for this name"
        else:
            worst_holding, worst_corr = worst_pair(pairs, controls.basis)
            if worst_corr is None:
                note = f"no {controls.basis} correlation stored for this name"
            elif worst_corr <= controls.threshold:
                note = (f"no meaningful correlation to holdings (max {worst_corr:.2f} "
                        f"{controls.basis} with {worst_holding}, at or below "
                        f"{controls.threshold:.2f})")
            else:
                correlation_scale, reduction = PIPELINE.sizing_scale(
                    worst_corr, controls.threshold, controls.reduction_factor,
                    flat=controls.flat)
                correlation_adjusted = True
                note = (f"correlation {worst_corr:.2f} ({controls.basis}) with holding "
                        f"{worst_holding} above {controls.threshold:.2f}: size cut "
                        f"{reduction * 100:.0f}%")

    total = correlation_scale * conviction_scale
    adjusted_guide = (PIPELINE.apply_reduction(base_guide, total)
                      if base_guide and total != 1.0 else base_guide)

    return Sized(
        ticker=candidate.get("ticker"),
        name=candidate.get("name"),
        sector=candidate.get("sector"),
        composite=_num(candidate.get("composite")),
        already_held=bool(candidate.get("already_held")),
        base_guide=base_guide,
        base_low=base_low,
        base_high=base_high,
        adjusted_guide=adjusted_guide,
        adjusted_low=None if base_low is None else round(base_low * total, 2),
        adjusted_high=None if base_high is None else round(base_high * total, 2),
        scale=round(total, 4),
        correlation_scale=round(correlation_scale, 4),
        conviction_scale=round(conviction_scale, 4),
        conviction=sizing.get("conviction"),
        max_correlation=worst_corr,
        correlated_with=worst_holding,
        correlation_adjusted=correlation_adjusted,
        note=note,
    )


def resize_all(candidates, controls: Controls, include_held: bool = False):
    """Re-size the shortlist. Held names are excluded unless asked for.

    The simulator is about what to buy, and position_sizer does not size a name
    you already own as a new position, so the default view is the not-yet-held
    set.
    """
    out = []
    for candidate in candidates:
        if candidate.get("already_held") and not include_held:
            continue
        out.append(resize(candidate, controls))
    return out


# ── integrity check ───────────────────────────────────────────────────────

def verify(candidates, params: dict, tolerance: float = 0.011):
    """Re-size at the file's own params and check the file comes back.

    This is the claim the whole simulator rests on: that recomputing from the
    stored correlations reproduces what position_sizer.py wrote. If a future
    change to position_sizer's sizing makes that false, this is what says so,
    instead of the dashboard quietly showing numbers the pipeline would not.

    The tolerance is one unit in the last place of the stored values, which are
    rounded to two decimals.
    """
    if not PIPELINE.ok:
        return {"ran": False, "reason": "position_sizer.py could not be imported"}

    controls = controls_from_params(params)
    checked, skipped, mismatches = 0, 0, []
    for candidate in candidates:
        stored = candidate.get("sizing") or {}
        if stored.get("adjusted_high_pct") is None:
            # "Avoid; research only." carries no percentage range to check.
            skipped += 1
            continue
        try:
            got = resize(candidate, controls)
        except Exception as exc:                      # noqa: BLE001
            mismatches.append({"ticker": candidate.get("ticker"),
                               "detail": f"{type(exc).__name__}: {exc}"})
            continue
        checked += 1
        for field, recomputed in (("adjusted_low_pct", got.adjusted_low),
                                  ("adjusted_high_pct", got.adjusted_high)):
            expected = _num(stored.get(field))
            if expected is None or recomputed is None:
                continue
            if abs(expected - recomputed) > tolerance:
                mismatches.append({
                    "ticker": candidate.get("ticker"),
                    "detail": f"{field}: file {expected}, recomputed {recomputed}",
                })
    return {"ran": True, "checked": checked, "skipped": skipped,
            "mismatches": mismatches, "ok": not mismatches, "controls": controls}


# ── rollup over a selection ───────────────────────────────────────────────

def rollup(selection, scan=None) -> dict:
    """Totals for a handful of candidates considered together.

    Everything here is a percentage of the account, which is currency-neutral -
    a 3% position is 3% whether it is quoted in CAD or USD. The currency mix is
    still reported, because the person placing these orders is converting real
    money and the pipeline's own discipline is to name a mixed-currency rollup
    rather than let it pass unremarked.
    """
    low = sum(s.adjusted_low for s in selection if s.adjusted_low is not None)
    high = sum(s.adjusted_high for s in selection if s.adjusted_high is not None)
    base_low = sum(s.base_low for s in selection if s.base_low is not None)
    base_high = sum(s.base_high for s in selection if s.base_high is not None)

    by_sector = {}
    for item in selection:
        if item.adjusted_high is None:
            continue
        sector = item.sector or "Unknown"
        by_sector[sector] = by_sector.get(sector, 0.0) + item.adjusted_high

    # Which holdings are taking the most size out of this selection, in
    # percentage points rather than in scale factors - a 40% cut on a 1% name
    # and a 10% cut on an 8% name are not the same amount of foregone position.
    by_holding = {}
    for item in selection:
        if not item.correlation_adjusted or not item.correlated_with:
            continue
        points = item.points_given_up
        if points is None:
            continue
        entry = by_holding.setdefault(item.correlated_with,
                                      {"points": 0.0, "names": []})
        entry["points"] += points
        entry["names"].append(item.ticker)

    currencies = {}
    if scan is not None:
        for item in selection:
            currency = None
            for candidate in scan.candidates:
                if candidate.get("ticker") == item.ticker:
                    currency = candidate.get("quote_currency")
                    break
            currencies.setdefault(str(currency or "unknown").upper(), []).append(item.ticker)

    return {
        "count": len(selection),
        "low": round(low, 2),
        "high": round(high, 2),
        "base_low": round(base_low, 2),
        "base_high": round(base_high, 2),
        "points_given_up": round(base_high - high, 2),
        "by_sector": dict(sorted(by_sector.items(), key=lambda kv: -kv[1])),
        "by_holding": dict(sorted(by_holding.items(),
                                  key=lambda kv: -kv[1]["points"])),
        "currencies": currencies,
        "mixed_currency": len(currencies) > 1,
    }
