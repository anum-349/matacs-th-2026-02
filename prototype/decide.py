"""Step 3 of the flow: decide. Plain code, no LLM.

Input : the criteria config and extraction.json (the output of step 2).
Output: per proposal one of ranked / flagged / ineligible, a score breakdown with the
        evidence quote for every number used, and a ranked list.

Rules (all driven by the config, nothing is hard-coded to one category):
  * Mandatory criteria: a stated "no" makes a proposal ineligible. Not stated, or a value
    that failed verification in step 2, makes it flagged. Unknown is never treated as a fail.
  * Limits (budget ceiling, schedule): over the limit is ineligible.
  * Weighted criteria: linear interpolation between the admin's "full marks" and "zero" values.
  * A flagged proposal is never ranked automatically. It carries a provisional score so the
    reviewer can see where it would land. A human can clear its flags (see app.py).
Same extraction + same config + same date always gives the same result.
"""
from __future__ import annotations

import math
from datetime import date

_WEEKS = {"weeks": 1.0, "days": 1 / 7, "months": 52 / 12}


def to_weeks(d) -> float | None:
    """Convert {"number", "unit"} to weeks, rounded UP to 0.1 so rounding never helps a bidder."""
    if not isinstance(d, dict) or d.get("unit") not in _WEEKS:
        return None
    return math.ceil(round(d["number"] * _WEEKS[d["unit"]] * 10, 6)) / 10


def part(value, full, zero) -> float:
    """0..1 between the zero point and the full-marks point. Unknown earns 0."""
    if value is None:
        return 0.0
    return max(0.0, min(1.0, (value - zero) / (full - zero)))


class _Ctx:
    """Reads fields from one proposal's extraction and collects flags and notes."""

    def __init__(self, fields: dict):
        self.fields = fields or {}
        self.flags: list[str] = []
        self.notes: list[str] = []
        self.flagged_fields: set[str] = set()

    def read(self, key, transform=None):
        """Return (value, state, field). state is 'ok', 'missing' or 'dropped'."""
        f = self.fields.get(key) or {}
        status = f.get("status", "not_stated")
        if status == "dropped":
            return None, "dropped", f
        if status != "ok":
            return None, "missing", f
        v = f.get("value")
        if transform == "weeks":
            v = to_weeks(v)
        return v, "ok", f

    def flag(self, msg, field=None):
        if msg not in self.flags:
            self.flags.append(msg)
        if field:
            self.flagged_fields.add(field)

    def note(self, msg):
        if msg not in self.notes:
            self.notes.append(msg)

    def unusable(self, label, field, state, f, policy):
        """Flag a dropped value always; flag a missing one only when the config says so."""
        if field in self.flagged_fields:
            return
        if state == "dropped":
            self.flag(f"{label}: the extracted value failed verification ({f.get('reason')})", field)
        elif state == "missing" and policy == "flag":
            self.flag(f"{label}: not stated in the proposal", field)


def _ev(key, f) -> dict:
    return {"field": key, "value": f.get("value"), "quote": f.get("quote"),
            "page": f.get("page"), "status": f.get("status", "not_stated")}


def _out(status, total, reason, ctx=None, criteria=None, checks=None, price=None):
    return {"status": status, "total": total, "reason": reason,
            "flags": list(ctx.flags) if ctx else [], "notes": list(ctx.notes) if ctx else [],
            "checks": checks or [], "criteria": criteria or [], "price": price}


def _is_present(v) -> bool:
    if isinstance(v, bool):
        return v
    return isinstance(v, (int, float)) and v > 0


def _qualifier(c, ctx, today):
    q = c["qualifier"]
    size, st, _ = ctx.read(q["min_size_field"])
    if st == "ok":
        if size < q["min_kw"]:
            ctx.flag(f"{c['label']}: count may include installs under {q['min_kw']:g} kW "
                     f"(smallest stated: {size:g} kW)")
    else:
        ctx.note(f"{c['label']}: install size not confirmed against the {q['min_kw']:g} kW threshold")
    since, st, _ = ctx.read(q["since_field"])
    if st == "ok" and since < today.year - q["window_years"]:
        ctx.note(f"{c['label']}: counted 'since {since:g}', which may include installs "
                 f"outside the {q['window_years']:g}-year window")


def _score(c, ctx, today):
    """Return (fraction 0..1, evidence list) for one weighted criterion."""
    label, policy = c["label"], c.get("missing")
    evidence = []
    if c["kind"] == "linear":
        v, st, f = ctx.read(c["field"], c.get("transform"))
        evidence.append(_ev(c["field"], f))
        ctx.unusable(label, c["field"], st, f, policy)
        if c.get("qualifier") and st == "ok":
            _qualifier(c, ctx, today)
        return part(v, c["full"], c["zero"]), evidence
    if c["kind"] == "composite":
        frac = 0.0
        for comp in c["components"]:
            v, st, f = ctx.read(comp["field"])
            evidence.append(_ev(comp["field"], f))
            ctx.unusable(label, comp["field"], st, f, policy)
            frac += comp["share"] * part(v, comp["full"], comp["zero"])
        return frac, evidence
    earned = 0.0                                            # kind == "points"
    for it in c["items"]:
        v, st, f = ctx.read(it["field"])
        evidence.append(_ev(it["field"], f))
        ctx.unusable(label, it["field"], st, f, policy)
        if st == "ok" and _is_present(v):
            earned += it["points"]
    return earned / sum(it["points"] for it in c["items"]), evidence


def evaluate(cfg: dict, ex: dict, today: date | None = None) -> dict:
    today = today or date.today()
    if ex.get("status") != "extracted":
        why = ex.get("error") or ex.get("reason") or "no usable extraction"
        res = _out("flagged", None, f"Could not be assessed: {why}")
        res["flags"] = [f"not assessed: {why}"]
        return res

    ctx = _Ctx(ex.get("fields"))
    inj = ex.get("injection") or {}
    if inj.get("suspected"):
        ctx.flag("contains text aimed at automated reviewers or AI (ignored, not scored)")

    checks, fails = [], []

    for m in cfg["mandatory"]:
        v, st, f = ctx.read(m["field"])
        if st == "dropped":
            ctx.flag(f"{m['label']}: the extracted value failed verification ({f.get('reason')})", m["field"])
            outcome = "unverified"
        elif st == "missing":
            ctx.flag(f"{m['label']}: not confirmed in the proposal", m["field"])
            outcome = "unknown"
        else:
            ok = (v is True) if m["test"] == "is_true" else (v >= m["value"])
            outcome = "pass" if ok else "fail"
            if not ok:
                q = f.get("quote")
                fails.append(f"Fails mandatory criterion: {m['label']}"
                             + (f' (proposal says: "{q[:120]}")' if q else ""))
        checks.append({"id": m["id"], "label": m["label"], "outcome": outcome, **_ev(m["field"], f)})

    for lim in cfg["limits"]:
        v, st, f = ctx.read(lim["field"], lim.get("transform"))
        if st == "dropped":
            ctx.flag(f"{lim['label']}: the extracted value failed verification ({f.get('reason')})", lim["field"])
            outcome = "unverified"
        elif st == "missing":
            ctx.flag(f"{lim['label']} cannot be verified: not stated in the proposal", lim["field"])
            outcome = "unknown"
        else:
            over = v > lim["max"]
            outcome = "fail" if over else "pass"
            if over:
                fails.append(f"{lim['label']} exceeded: {v:g} {lim['unit']} (limit {lim['max']:g})")
        checks.append({"id": lim["id"], "label": lim["label"], "outcome": outcome, **_ev(lim["field"], f)})

    price_v, price_st, _ = ctx.read("price_cad")
    price = price_v if price_st == "ok" else None

    if fails:
        return _out("ineligible", None, " ".join(fails), ctx, checks=checks, price=price)

    criteria, total = [], 0.0
    for c in cfg["weighted"]:
        frac, evidence = _score(c, ctx, today)
        pts = c["weight"] * frac
        total += pts
        criteria.append({"id": c["id"], "label": c["label"], "weight": c["weight"],
                         "points": round(pts, 1), "evidence": evidence})
    total = round(total, 1)
    breakdown = ", ".join(f"{c['id']} {c['points']:.1f}/{c['weight']:g}" for c in criteria)

    if ctx.flags:
        reason = "; ".join(ctx.flags) + f". Not ranked; provisional score {total} ({breakdown})"
        return _out("flagged", total, reason, ctx, criteria, checks, price)
    return _out("ranked", total, f"Score {total}: {breakdown}", ctx, criteria, checks, price)


def referenced_fields(cfg: dict) -> set[str]:
    keys = {m["field"] for m in cfg["mandatory"]} | {l["field"] for l in cfg["limits"]}
    for c in cfg["weighted"]:
        if "field" in c:
            keys.add(c["field"])
        keys |= {x["field"] for x in c.get("components", [])}
        keys |= {x["field"] for x in c.get("items", [])}
        if c.get("qualifier"):
            keys |= {c["qualifier"]["min_size_field"], c["qualifier"]["since_field"]}
    return keys


def missing_fields(cfg: dict, doc: dict) -> list[str]:
    """Fields the config needs that the stored extraction never asked for (config changed
    after extraction). Those need a re-extract; everything else is just a re-score."""
    have = set()
    for ex in doc["proposals"].values():
        if ex.get("status") == "extracted":
            have |= set(ex.get("fields", {}))
    if not have:
        return []
    return sorted(referenced_fields(cfg) - have)


def decide(cfg: dict, doc: dict, clearances: dict | None = None, today: date | None = None) -> dict:
    """Score every proposal and build the ranking.

    clearances: {proposal_id: {"reviewer", "reason", "sha256"}}. A flagged proposal with a
    clearance for the same file hash is allowed into the ranking; the reason says so."""
    clearances = clearances or {}
    gap = cfg.get("close_call_gap", 2.0)
    details = {}
    for pid in sorted(doc["proposals"]):
        ex = doc["proposals"][pid]
        res = evaluate(cfg, ex, today)
        fv = (ex.get("fields") or {}).get("bidder_name") or {}
        res["bidder"] = fv.get("value") if fv.get("status") == "ok" else None
        c = clearances.get(pid)
        if (c and res["status"] == "flagged" and res["total"] is not None
                and c.get("sha256") == ex.get("sha256")):
            res["status"] = "ranked"
            res["cleared_by"] = {"reviewer": c.get("reviewer"), "reason": c.get("reason")}
            res["reason"] = (f"Score {res['total']} (flags cleared by {c.get('reviewer')}: "
                             f"{c.get('reason')}; flags were: {'; '.join(res['flags'])})")
        details[pid] = res

    order = sorted((p for p in details if details[p]["status"] == "ranked"),
                   key=lambda p: (-details[p]["total"],
                                  details[p]["price"] if details[p]["price"] is not None else math.inf, p))
    ranking = []
    for i, pid in enumerate(order, 1):
        d = details[pid]
        close = i < len(order) and d["total"] - details[order[i]]["total"] < gap
        ranking.append({"proposal": pid, "rank": i, "status": "ranked", "score": d["total"],
                        "reason": d["reason"] + (" Close call with the next rank." if close else "")})
    for pid, d in details.items():
        if d["status"] != "ranked":
            ranking.append({"proposal": pid, "rank": None, "status": d["status"],
                            "score": d["total"], "reason": d["reason"]})
    versions = sorted({ex.get("config_version") for ex in doc["proposals"].values()
                       if ex.get("config_version") is not None})
    return {"config_version": cfg["version"], "extraction_config_versions": versions,
            "ranking": ranking, "details": details}