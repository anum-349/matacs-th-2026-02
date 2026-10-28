from __future__ import annotations

import json
import math
import re
from pathlib import Path

CRITERIA_DIR = Path(__file__).parent / "criteria"
FIELD_TYPES = ("string", "number", "boolean", "duration")


class UnknownCategory(Exception):
    pass


class BadConfig(Exception):
    pass


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def list_categories() -> list[str]:
    names = []
    for path in sorted(CRITERIA_DIR.glob("*.json")):
        try:
            names.append(json.loads(path.read_text(encoding="utf-8"))["category"])
        except (OSError, ValueError, KeyError):
            continue
    return names


def load_config(category: str) -> dict:
    """Look a category up by name (case and spacing do not matter) and validate it."""
    slug = slugify(category or "")
    path = CRITERIA_DIR / f"{slug}.json"        # slugify leaves only [a-z0-9-], so no path tricks
    if not slug or not path.is_file():
        raise UnknownCategory(category)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise BadConfig(f"{path.name} is not valid JSON") from e
    _validate(cfg, path.name)
    return cfg


def _validate(cfg: dict, filename: str) -> None:
    if not isinstance(cfg.get("category"), str) or not cfg["category"].strip():
        raise BadConfig(f"{filename}: 'category' is required")
    if not isinstance(cfg.get("version"), int):
        raise BadConfig(f"{filename}: 'version' must be an integer")
    fields = cfg.get("extraction_fields")
    if not isinstance(fields, list) or not fields:
        raise BadConfig(f"{filename}: 'extraction_fields' must be a non-empty list")

    seen = set()
    for f in fields:
        key = f.get("key") if isinstance(f, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise BadConfig(f"{filename}: every field needs a snake_case 'key'")
        if key in seen:
            raise BadConfig(f"{filename}: duplicate field key '{key}'")
        seen.add(key)
        if f.get("type") not in FIELD_TYPES:
            raise BadConfig(f"{filename}: field '{key}' has an unknown type")
        if not isinstance(f.get("definition"), str) or not f["definition"].strip():
            raise BadConfig(f"{filename}: field '{key}' needs a definition")

    _validate_scoring(cfg, filename, {f["key"]: f["type"] for f in fields})


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _validate_scoring(cfg: dict, filename: str, types: dict) -> None:
    def bad(msg):
        raise BadConfig(f"{filename}: {msg}")

    def field_of(key, allowed, where):
        if key not in types:
            bad(f"{where} refers to unknown field '{key}'")
        if types[key] not in allowed:
            bad(f"{where}: field '{key}' is {types[key]}, expected {' or '.join(allowed)}")

    def linear_ok(spec, where):
        if not (_num(spec.get("full")) and _num(spec.get("zero"))) or spec["full"] == spec["zero"]:
            bad(f"{where} needs numeric 'full' and 'zero' that differ")

    def transform_ok(spec, key, where):
        if types[key] == "duration" and spec.get("transform") != "weeks":
            bad(f"{where}: duration field '{key}' needs \"transform\": \"weeks\"")

    for section in ("mandatory", "limits", "weighted"):
        if not isinstance(cfg.get(section), list) or not cfg[section]:
            bad(f"'{section}' must be a non-empty list")

    ids = set()
    def uid(spec, where):
        i = spec.get("id")
        if not isinstance(i, str) or not i or i in ids:
            bad(f"{where} needs a unique 'id'")
        ids.add(i)
        if not isinstance(spec.get("label"), str) or not spec["label"].strip():
            bad(f"{where} '{i}' needs a label")

    for m in cfg["mandatory"]:
        uid(m, "mandatory rule")
        if m.get("test") == "is_true":
            field_of(m.get("field"), ("boolean",), f"mandatory '{m['id']}'")
        elif m.get("test") == "min":
            field_of(m.get("field"), ("number",), f"mandatory '{m['id']}'")
            if not _num(m.get("value")):
                bad(f"mandatory '{m['id']}' needs a numeric 'value'")
        else:
            bad(f"mandatory '{m['id']}' has unknown test")

    for l in cfg["limits"]:
        uid(l, "limit")
        field_of(l.get("field"), ("number", "duration"), f"limit '{l['id']}'")
        transform_ok(l, l["field"], f"limit '{l['id']}'")
        if not _num(l.get("max")):
            bad(f"limit '{l['id']}' needs a numeric 'max'")

    total = 0
    for c in cfg["weighted"]:
        uid(c, "weighted criterion")
        w = c.get("weight")
        if not _num(w) or w <= 0:
            bad(f"criterion '{c['id']}' needs a positive weight")
        total += w
        if c.get("missing") not in (None, "flag", "zero"):
            bad(f"criterion '{c['id']}': 'missing' must be \"flag\" or \"zero\"")
        where = f"criterion '{c['id']}'"
        kind = c.get("kind")
        if kind == "linear":
            field_of(c.get("field"), ("number", "duration"), where)
            transform_ok(c, c["field"], where)
            linear_ok(c, where)
            q = c.get("qualifier")
            if q:
                field_of(q.get("min_size_field"), ("number",), where)
                field_of(q.get("since_field"), ("number",), where)
                if not (_num(q.get("min_kw")) and _num(q.get("window_years"))):
                    bad(f"{where}: qualifier needs numeric min_kw and window_years")
        elif kind == "composite":
            comps = c.get("components")
            if not isinstance(comps, list) or not comps:
                bad(f"{where} needs components")
            for comp in comps:
                field_of(comp.get("field"), ("number",), where)
                linear_ok(comp, where)
                if not _num(comp.get("share")):
                    bad(f"{where}: each component needs a numeric 'share'")
            if not math.isclose(sum(x["share"] for x in comps), 1.0, abs_tol=1e-9):
                bad(f"{where}: component shares must add up to 1")
        elif kind == "points":
            items = c.get("items")
            if not isinstance(items, list) or not items:
                bad(f"{where} needs items")
            for it in items:
                field_of(it.get("field"), ("boolean", "number"), where)
                if not _num(it.get("points")) or it["points"] <= 0:
                    bad(f"{where}: each item needs positive points")
        else:
            bad(f"{where} has unknown kind")
    if not math.isclose(total, 100, abs_tol=1e-9):
        bad(f"weights add up to {total:g}, must be 100")
    gap = cfg.get("close_call_gap", 2.0)
    if not _num(gap) or gap < 0:
        bad("'close_call_gap' must be a non-negative number")