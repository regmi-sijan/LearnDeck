"""HTTP API + static web UI (FastAPI)."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .engine import Engine
from .ingest import IngestError, read_files, read_url, soffice_path
from .llm import LLMError, get_llm
from .store import DeckStore

app = FastAPI(title="LearnDeck", docs_url="/api/docs", redoc_url=None)
store = DeckStore()
engine = Engine(store)


# ---------------------------------------------------------------- helpers
def _deck(deck_id: str):
    deck = store.get(deck_id)
    if not deck:
        raise HTTPException(404, "Deck not found")
    return deck


def _node(deck, nid: str):
    node = deck.node(nid)
    if not node:
        raise HTTPException(404, "Slide not found")
    return node


def _brief(deck, nid: str) -> dict:
    n = deck.node(nid)
    return {"id": n["id"], "title": n["title"], "icon": n.get("icon", ""), "summary": n.get("summary", ""),
            "status": n["status"], "visited": n.get("visited", False), "kind": n["kind"], "depth": n["depth"],
            "has_children": bool(n["children"]) or n["status"] != "ready"}


def _full(deck, nid: str) -> dict:
    n = deck.node(nid)
    parent = deck.node(n["parent"]) if n.get("parent") else None
    siblings = [s for s in parent["children"] if deck.node(s)] if parent else [n["id"]]
    srcs = [deck.chunks[i] for i in n["sources"][:60] if i in deck.chunks]
    return {
        **{k: n.get(k) for k in ("id", "parent", "title", "summary", "icon", "depth", "kind", "status", "error",
                                 "progress", "content", "visited", "qa", "selection", "provider")},
        "children": [_brief(deck, c) for c in n["children"] if deck.node(c)],
        "path": [{"id": p["id"], "title": p["title"], "icon": p.get("icon", "")} for p in deck.path_to(nid)],
        "siblings": [{"id": s, "title": deck.node(s)["title"]} for s in siblings],
        "source_labels": [c.get("loc") or c["title"] for c in srcs[:8]],
        "source_count": len(n["sources"]),
        "max_depth": config.MAX_DEPTH,
    }


def _make_deck(ing, source: dict, level: str, originals=None):
    level = level if level in ("beginner", "intermediate", "expert") else "intermediate"
    deck = store.create(ing, source=source, level=level, originals=originals)
    engine.ensure(deck, "root")
    return {"id": deck.id, "title": deck.meta["title"], "kind": deck.meta["kind"], "notes": deck.meta["notes"]}


# ------------------------------------------------------------------- api
@app.get("/api/health")
def health():
    try:
        llm = get_llm().describe()
    except LLMError as e:
        llm = {"provider": "none", "error": str(e)}
    return {"ok": True, "llm": llm, "libreoffice": bool(soffice_path()), "max_upload_mb": config.MAX_UPLOAD_MB}


class SettingsIn(BaseModel):
    provider: str = "auto"
    api_key: Optional[str] = None
    model: Optional[str] = None
    ollama_url: Optional[str] = None


@app.get("/api/settings")
def get_settings():
    s = config.llm_settings()
    return {**s, "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
            "openai_key_set": bool(os.getenv("OPENAI_API_KEY")), "active": health()["llm"]}


@app.post("/api/settings")
def save_settings(body: SettingsIn):
    provider = body.provider.strip().lower()
    if provider not in ("auto", "anthropic", "openai", "ollama", "none"):
        raise HTTPException(400, "Unknown provider")
    updates = {"LLM_PROVIDER": provider}
    if body.api_key and body.api_key.strip():
        key = body.api_key.strip()
        if provider in ("anthropic", "openai"):
            target = f"{provider.upper()}_API_KEY"
        else:
            target = "ANTHROPIC_API_KEY" if key.startswith("sk-ant-") else "OPENAI_API_KEY"
        updates[target] = key
    if body.model and body.model.strip():
        name = {"anthropic": "ANTHROPIC_MODEL", "openai": "OPENAI_MODEL", "ollama": "OLLAMA_MODEL"}.get(provider)
        if name:
            updates[name] = body.model.strip()
    if body.ollama_url and body.ollama_url.strip():
        updates["OLLAMA_URL"] = body.ollama_url.strip()
    for k, v in updates.items():
        if re.search(r"[\r\n]", v):
            raise HTTPException(400, "Invalid value")
        os.environ[k] = v
    _write_env(updates)
    return get_settings()


def _write_env(updates: dict) -> None:
    path: Path = config.ENV_FILE
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}"
            seen.add(m.group(1))
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n", "utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


@app.get("/api/decks")
def list_decks():
    return store.list()


@app.post("/api/decks/upload")
async def upload(files: list[UploadFile] = File(...), level: str = Form("intermediate")):
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    batch, total = [], 0
    for f in files:
        data = await f.read()
        total += len(data)
        if total > limit:
            raise HTTPException(413, f"Upload is larger than {config.MAX_UPLOAD_MB} MB.")
        name = (f.filename or "file").replace("\\", "/")
        batch.append((name, data))
    if not batch:
        raise HTTPException(400, "No files received.")
    try:
        ing = read_files(batch, as_repo=len(batch) > 1)
    except IngestError as e:
        raise HTTPException(422, str(e))
    if not ing.chunks:
        raise HTTPException(422, "No readable content was found.")
    names = [n for n, _ in batch]
    source = {"type": "file", "name": names[0] if len(names) == 1 else f"{len(names)} files", "files": names[:50]}
    return _make_deck(ing, source, level, originals=batch)


class LinkIn(BaseModel):
    url: str
    level: str = "intermediate"


@app.post("/api/decks/link")
def from_link(body: LinkIn):
    try:
        ing = read_url(body.url)
    except IngestError as e:
        raise HTTPException(422, str(e))
    if not ing.chunks:
        raise HTTPException(422, "No readable content was found at that link.")
    return _make_deck(ing, {"type": "url", "name": body.url, "url": body.url}, body.level)


class TextIn(BaseModel):
    text: str
    title: str = "Pasted text"
    level: str = "intermediate"


@app.post("/api/decks/text")
def from_text(body: TextIn):
    if not body.text.strip():
        raise HTTPException(400, "The text is empty.")
    name = re.sub(r"[^\w\- ]+", "", body.title).strip() or "pasted"
    try:
        ing = read_files([(name + ".md", body.text.encode("utf-8"))])
    except IngestError as e:
        raise HTTPException(422, str(e))
    return _make_deck(ing, {"type": "text", "name": body.title}, body.level)


@app.get("/api/decks/{deck_id}")
def deck_info(deck_id: str):
    deck = _deck(deck_id)
    nodes = {nid: {"id": nid, "parent": n.get("parent"), "title": n["title"], "icon": n.get("icon", ""),
                   "children": n["children"], "status": n["status"], "visited": n.get("visited", False),
                   "kind": n["kind"]} for nid, n in deck.tree["nodes"].items()}
    return {**deck.meta, "root": deck.tree["root"], "nodes": nodes, "progress": deck.progress(),
            "llm": health()["llm"]}


@app.delete("/api/decks/{deck_id}")
def delete_deck(deck_id: str):
    if not store.delete(deck_id):
        raise HTTPException(404, "Deck not found")
    return {"ok": True}


@app.get("/api/decks/{deck_id}/nodes/{nid}")
def get_node(deck_id: str, nid: str, retry: bool = False):
    deck = _deck(deck_id)
    node = _node(deck, nid)
    engine.ensure(deck, node["id"], retry=retry)
    return _full(deck, node["id"])


@app.post("/api/decks/{deck_id}/nodes/{nid}/visited")
def mark_visited(deck_id: str, nid: str):
    deck = _deck(deck_id)
    node = _node(deck, nid)
    if not node.get("visited"):
        with deck.lock:
            node["visited"] = True
        deck.save_tree()
    return deck.progress()


class DiveIn(BaseModel):
    text: str


@app.post("/api/decks/{deck_id}/nodes/{nid}/dive")
def dive(deck_id: str, nid: str, body: DiveIn):
    deck = _deck(deck_id)
    node = _node(deck, nid)
    if len(body.text.strip()) < 2:
        raise HTTPException(400, "Select a bit more text.")
    child = engine.dive(deck, node["id"], body.text)
    return {"id": child["id"]}


class AskIn(BaseModel):
    question: str


@app.post("/api/decks/{deck_id}/nodes/{nid}/ask")
def ask(deck_id: str, nid: str, body: AskIn):
    deck = _deck(deck_id)
    node = _node(deck, nid)
    if not body.question.strip():
        raise HTTPException(400, "Type a question first.")
    try:
        return {"answer": engine.ask(deck, node["id"], body.question)}
    except LLMError as e:
        raise HTTPException(502, str(e))


@app.post("/api/decks/{deck_id}/nodes/{nid}/regenerate")
def regenerate(deck_id: str, nid: str):
    deck = _deck(deck_id)
    node = _node(deck, nid)
    engine.regenerate(deck, node["id"])
    return {"ok": True}


@app.get("/api/decks/{deck_id}/nodes/{nid}/sources")
def sources(deck_id: str, nid: str):
    deck = _deck(deck_id)
    node = _node(deck, nid)
    out = []
    for cid in node["sources"][:40]:
        c = deck.chunks.get(cid)
        if not c:
            continue
        item = {"id": cid, "title": c["title"], "loc": c.get("loc", ""), "lang": c.get("lang"),
                "text": c["text"][:20000], "truncated": len(c["text"]) > 20000}
        if c.get("preview") and c.get("page") and deck.preview_file(c["preview"]).exists():
            item["page_url"] = f"/api/decks/{deck.id}/preview/{c['preview']}/{c['page']}.png"
        if c.get("image") and deck.image_file(c["image"]).exists():
            item["image_url"] = f"/api/decks/{deck.id}/image/{c['image']}"
        out.append(item)
    return {"items": out, "total": len(node["sources"])}


@app.get("/api/decks/{deck_id}/preview/{key}/{page}.png")
def preview_page(deck_id: str, key: str, page: int):
    deck = _deck(deck_id)
    png = engine.render_page(deck, re.sub(r"[^\w\-]", "", key), page)
    if not png:
        raise HTTPException(404, "No preview")
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/decks/{deck_id}/image/{name}")
def image(deck_id: str, name: str):
    deck = _deck(deck_id)
    f = deck.image_file(name)
    if not f.exists():
        raise HTTPException(404, "No image")
    return FileResponse(f)


@app.exception_handler(LLMError)
async def llm_error(_, exc: LLMError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


# Static UI last so /api routes win.
app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
