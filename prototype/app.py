from __future__ import annotations
import hashlib
import json
import os
import datetime as dt
import re
import uuid

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename
from pathlib import Path

from ingest import IngestResult, ingest_pdf
import extract as extract_module
import decide as decide_module
from criteria import BadConfig, UnknownCategory, list_categories, load_config

DATA_DIR = Path(os.getenv("DATA_DIR", 'data'))
MAX_FILES = int(os.getenv("MAX_FILES", 50))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024 #whole request

@app.errorhandler(413)
def too_large(_):
    return jsonify(error='Upload file is too large to process.'), 413

def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')

def _audit(event: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR/'audit.jsonl', 'a', encoding='utf-8') as f:
        f.write(json.dumps({"ts": _now(), **event}, ensure_ascii=False) + '\n')

def _write_json(path: Path, doc) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _split_text_proposals(raw_text: str) -> list[str]:
    """Split pasted proposal text into one block per proposal.

    A user can paste proposals separated by headings such as:
    --- Proposal A ---
    or
    === Proposal 1 ===
    If there are no markers, the whole blob is treated as one proposal.
    """
    text = (raw_text or "").strip()
    if not text:
        return []

    try:
        payload = json.loads(text)
    except ValueError:
        payload = None

    if isinstance(payload, list):
        items = []
        for item in payload:
            if isinstance(item, str) and item.strip():
                items.append(item.strip())
        if items:
            return items

    if isinstance(payload, dict):
        collections = [payload.get("proposals"), payload.get("items"), payload.get("data")]
        for candidate in collections:
            if isinstance(candidate, list):
                items = [str(item).strip() for item in candidate if isinstance(item, str) and item.strip()]
                if items:
                    return items

    patterns = [
        r"(?im)^\s*(?:[-*_]{3,}\s*)?(?:proposal|bid)\s*[A-Za-z0-9]+\s*(?:[-*_]{3,})?\s*$",
        r"(?im)^\s*(?:[-*_]{3,}\s*)?===\s*(?:proposal|bid)\s*[A-Za-z0-9]+\s*===\s*(?:[-*_]{3,})?\s*$",
        r"(?im)^\s*(?:[-*_]{3,}\s*)?---\s*(?:proposal|bid)\s*[A-Za-z0-9]+\s*---\s*(?:[-*_]{3,})?\s*$",
    ]
    compiled = [re.compile(p) for p in patterns]

    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if any(p.fullmatch(line.strip()) for p in compiled):
            if current:
                joined = "\n".join(current).strip()
                if joined:
                    blocks.append(joined)
                current = []
            continue
        current.append(line)

    if current:
        joined = "\n".join(current).strip()
        if joined:
            blocks.append(joined)

    return blocks or [text]


def _text_run_to_ingest_result(run_id: str, proposal_idx: int, text: str):
    name = f"proposal-{proposal_idx}.txt"
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    res = IngestResult(
        filename=name,
        sha256=digest,
        size_bytes=len(data),
        status="ok",
        reason=None,
        page_count=1,
        pages=[text],
    )
    return res

_RUN_ID_RE = re.compile(r"[0-9a-f]{12}")

def _run_dir_or_error(run_id):
    """Return (run_dir, None) or (None, error_response)."""
    if not _RUN_ID_RE.fullmatch(run_id):
        return None, (jsonify(error='Invalid run id'), 400)
    run_dir = DATA_DIR / "runs" / run_id
    if not (run_dir / "ingest.json").is_file():
        return None, (jsonify(error="Run not found"), 404)
    return run_dir, None

@app.post("/api/runs")
def create_run():
    category = (request.form.get("category") or "").strip()
    files = [f for f in request.files.getlist("files") if f and f.filename]
    raw_text = (request.form.get("proposal_texts") or "").strip()

    if not category:
        return jsonify(error='"Category" is required.'), 400

    try:
        cfg = load_config(category)
    except UnknownCategory:
        return jsonify(error=f"Unknown category '{category}'.", categories=list_categories()), 400
    except BadConfig as e:
        return jsonify(error=f"Criteria config problem: {e}"), 500

    proposal_blocks = _split_text_proposals(raw_text)
    if not files and not proposal_blocks:
        return jsonify(error="At least one PDF file or extracted proposal text block is required."), 400

    total_input_count = len(files) + len(proposal_blocks)
    if total_input_count > MAX_FILES:
        return jsonify(error=f"At most {MAX_FILES} files per run allowed."), 400

    run_id = uuid.uuid4().hex[:12]
    run_dir = DATA_DIR / "runs" / run_id

    (run_dir / "files").mkdir(parents=True)

    first_seen: dict[str, str] = {}
    proposals = []
    records = []
    for i, f in enumerate(files, start=1):
        proposal_id  = f"PROP{i:02d}"
        data = f.read()
        name = secure_filename(f.filename) or "unnamed.pdf"
        digest = hashlib.sha256(data).hexdigest()

        if digest in first_seen:
            res = IngestResult(
                filename=name, sha256=digest, size_bytes=len(data),
                reason=f"Duplicate of {first_seen[digest]}.",
            )
        else:
            res = ingest_pdf(name, data)
            first_seen[digest] = proposal_id
            (run_dir / "files" / f"{digest}.bin").write_bytes(data)

        summary = {"proposal_id": proposal_id, **res.summary()}
        proposals.append(summary)
        records.append({**summary, "pages": res.pages})

        _audit({"event": "file_ingested", "run_id": run_id, **summary})

    for i, block in enumerate(proposal_blocks, start=1):
        proposal_index = len(files) + i
        proposal_id = f"PROP{proposal_index:02d}"
        res = _text_run_to_ingest_result(run_id, proposal_index, block)
        digest = res.sha256

        if digest in first_seen:
            res = IngestResult(
                filename=res.filename, sha256=digest, size_bytes=len(block.encode("utf-8")),
                reason=f"Duplicate of {first_seen[digest]}.",
            )
        else:
            first_seen[digest] = proposal_id
            (run_dir / "files" / f"{digest}.bin").write_text(block, encoding="utf-8")

        summary = {"proposal_id": proposal_id, **res.summary()}
        proposals.append(summary)
        records.append({**summary, "pages": res.pages})

        _audit({"event": "file_ingested", "run_id": run_id, **summary})

    _write_json(run_dir / "ingest.json", {
        "run_id": run_id,
        "category": category,
        "created": _now(),
        "proposals": records
    })

    _audit({"event": "run_created", "run_id": run_id, "category": cfg["category"],
            "config_version": cfg["version"], "files": total_input_count})

    total_ok = sum(1 for p in proposals if p["status"] == "ok")

    return jsonify(
        run_id = run_id,
        category = category,
        ready = total_ok,
        flagged = len(proposals) - total_ok,
        needs_check = sum(1 for p in proposals if p["warnings"]),
        proposals = proposals
    ), 201

@app.post("/api/runs/<run_id>/extract")
def extract_run(run_id):
    run_dir, err = _run_dir_or_error(run_id)
    if err:
        return err

    out_path = run_dir / "extraction.json"
    if out_path.exists() and request.args.get("force") != "1":
        return jsonify(error="This run was already extracted. pass force=1 to run it again"), 409

    run = json.loads((run_dir / "ingest.json").read_text(encoding="utf-8"))
    try:
        cfg = load_config(run["category"])
    except (UnknownCategory, BadConfig) as e:
        return jsonify(error=f"Criteria config problem: {e}"), 500

    results = {}

    for rec in run["proposals"]:
        pid = rec["proposal_id"]
        if rec["status"] != "ok":
            results[pid] = {"status": "skipped", "reason": rec["reason"]}
            continue
        try:
            res = extract_module.extract_fields(cfg, rec["pages"])
        except extract_module.LlmNotConfigured as e:
            return jsonify(error=str(e)), 503

        results[pid] = {**res.to_dict(), "sha256": rec["sha256"]}

        counts = {}
        for f in res.fields.values():
            counts[f["status"]] = counts.get(f["status"], 0) + 1
        _audit({"event": "extracted", "run_id": run_id, "proposal_id": pid,
                "sha256": rec["sha256"], "status": res.status, "error": res.error,
                "model": res.model, "prompt_version": res.prompt_version,
                "config_version": res.config_version, "field_counts": counts,
                "injection_suspected": res.injection.get("suspected")})

    _write_json(out_path, {"run_id": run_id, "category": cfg["category"],
                           "created": _now(), "proposals": results})

    def brief(r):
        return {k: v for k, v in r.items() if k != "attempts"}

    return jsonify(
        run_id= run_id,
        extracted = sum(1 for r in results.values() if r["status"] == "extracted"),
        failed = sum(1 for r in results.values() if r["status"]=="failed"),
        skipped = sum(1 for r in results.values() if r["status"]=="skipped"),
        proposals = {pid: brief(r) for pid, r in results.items()}
    ), 200

# ---------- step 3: decide ----------

def _load_clearances(run_dir: Path) -> dict:
    p = run_dir / "clearances.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}

@app.post("/api/runs/<run_id>/decide")
def decide_run(run_id):
    """Score and rank from the stored extraction. Safe to call again after the criteria
    config changes: it never re-extracts, and every call is saved as a new numbered decision."""
    run_dir, err = _run_dir_or_error(run_id)
    if err:
        return err
    ext_path = run_dir / "extraction.json"
    if not ext_path.is_file():
        return jsonify(error="Run has not been extracted yet. POST /extract first."), 409

    doc = json.loads(ext_path.read_text(encoding="utf-8"))
    try:
        cfg = load_config(doc["category"])
    except (UnknownCategory, BadConfig) as e:
        return jsonify(error=f"Criteria config problem: {e}"), 500

    missing = decide_module.missing_fields(cfg, doc)
    if missing:
        return jsonify(error="The criteria now need fields this run never extracted. "
                             "Re-run /extract?force=1 first.", missing_fields=missing), 409

    result = decide_module.decide(cfg, doc, _load_clearances(run_dir))

    decisions = run_dir / "decisions"
    decisions.mkdir(exist_ok=True)
    n = len(list(decisions.glob("decision-*.json"))) + 1
    out = {"run_id": run_id, "decision": n, "created": _now(), **result}
    _write_json(decisions / f"decision-{n:03d}.json", out)

    _audit({"event": "decided", "run_id": run_id, "decision": n,
            "config_version": cfg["version"],
            "extraction_config_versions": result["extraction_config_versions"],
            "ranking": [{k: r[k] for k in ("proposal", "rank", "status", "score")}
                        for r in result["ranking"]]})
    return jsonify(out), 200

@app.post("/api/runs/<run_id>/clear")
def clear_flags(run_id):
    """A reviewer takes responsibility for a flagged proposal's flags so it can be ranked.
    A written reason is required. Call /decide again afterwards."""
    run_dir, err = _run_dir_or_error(run_id)
    if err:
        return err
    ext_path = run_dir / "extraction.json"
    if not ext_path.is_file():
        return jsonify(error="Run has not been extracted yet."), 409

    body = request.get_json(silent=True) or {}
    pid = str(body.get("proposal_id") or "").strip()
    reviewer = str(body.get("reviewer") or "").strip()
    reason = str(body.get("reason") or "").strip()
    if not (pid and reviewer and reason):
        return jsonify(error="proposal_id, reviewer and reason are all required."), 400

    doc = json.loads(ext_path.read_text(encoding="utf-8"))
    ex = doc["proposals"].get(pid)
    if not ex or ex.get("status") != "extracted":
        return jsonify(error="That proposal has no extraction to clear."), 404

    clearances = _load_clearances(run_dir)
    clearances[pid] = {"reviewer": reviewer, "reason": reason, "ts": _now(), "sha256": ex.get("sha256")}
    _write_json(run_dir / "clearances.json", clearances)
    _audit({"event": "flags_cleared", "run_id": run_id, "proposal_id": pid,
            "reviewer": reviewer, "reason": reason, "sha256": ex.get("sha256")})
    return jsonify(ok=True, proposal_id=pid), 200

@app.route("/")
def home():
    return render_template("index.html")

if __name__=="__main__":
    app.run(debug=False, port=5000)