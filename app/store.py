"""Saving decks (document + lesson tree + progress) on disk under data/decks/<id>/."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from . import config
from .ingest import Ingested


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ]+", "_", name).strip(" .") or "file"
    return name[:120]


class Deck:
    """One learning deck: metadata, the parsed document, and the slide tree."""

    def __init__(self, path: Path):
        self.path = path
        self.id = path.name
        self.lock = threading.RLock()
        self.meta = json.loads((path / "deck.json").read_text("utf-8"))
        self.doc = json.loads((path / "doc.json").read_text("utf-8"))
        self.tree = json.loads((path / "tree.json").read_text("utf-8"))
        for node in self.tree["nodes"].values():  # jobs don't survive a restart
            if node.get("status") == "working":
                node["status"] = "pending"

    # --- persistence ------------------------------------------------------
    def save_tree(self) -> None:
        with self.lock:
            _write_json(self.path / "tree.json", self.tree)

    def save_doc(self) -> None:
        with self.lock:
            _write_json(self.path / "doc.json", self.doc)

    def save_meta(self) -> None:
        with self.lock:
            _write_json(self.path / "deck.json", self.meta)

    # --- convenience ------------------------------------------------------
    @property
    def chunks(self) -> dict:
        return self.doc["chunks"]

    def node(self, nid: str) -> dict | None:
        if nid == "root":
            nid = self.tree["root"]
        return self.tree["nodes"].get(nid)

    def new_node_id(self) -> str:
        with self.lock:
            self.tree["counter"] += 1
            return f"n{self.tree['counter']}"

    def path_to(self, nid: str) -> list[dict]:
        out, cur = [], self.node(nid)
        while cur is not None:
            out.append(cur)
            cur = self.tree["nodes"].get(cur.get("parent")) if cur.get("parent") else None
        return list(reversed(out))

    def progress(self) -> dict:
        nodes = self.tree["nodes"].values()
        return {"visited": sum(1 for n in nodes if n.get("visited")), "total": len(self.tree["nodes"])}

    def preview_file(self, key: str) -> Path:
        return self.path / "previews" / f"{_safe_name(key)}.pdf"

    def image_file(self, name: str) -> Path:
        return self.path / "images" / _safe_name(name)


class DeckStore:
    def __init__(self, root: Path | None = None):
        self.root = root or config.DECK_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self._decks: dict[str, Deck] = {}
        self._lock = threading.Lock()

    def get(self, deck_id: str) -> Deck | None:
        if not re.fullmatch(r"[\w-]+", deck_id or ""):
            return None
        with self._lock:
            if deck_id in self._decks:
                return self._decks[deck_id]
            path = self.root / deck_id
            if not (path / "tree.json").exists():
                return None
            deck = Deck(path)
            self._decks[deck_id] = deck
            return deck

    def create(self, ing: Ingested, *, source: dict, level: str,
               originals: list[tuple[str, bytes]] | None = None) -> Deck:
        deck_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        path = self.root / deck_id
        (path / "previews").mkdir(parents=True)
        (path / "images").mkdir()
        (path / "source").mkdir()
        for key, data in ing.previews.items():
            (path / "previews" / f"{_safe_name(key)}.pdf").write_bytes(data)
        for name, data in ing.images.items():
            (path / "images" / _safe_name(name)).write_bytes(data)
        for name, data in (originals or [])[:50]:
            (path / "source" / _safe_name(os.path.basename(name))).write_bytes(data)
        chunks = {c.id: c.to_dict() for c in ing.chunks}
        _write_json(path / "doc.json", {"chunks": chunks, "order": [c.id for c in ing.chunks],
                                        "outline": ing.outline})
        _write_json(path / "deck.json", {
            "id": deck_id, "title": ing.title, "kind": ing.kind, "created": time.time(),
            "level": level, "source": source, "notes": ing.notes,
            "chars": sum(len(c.text) for c in ing.chunks), "parts": len(ing.chunks),
        })
        root = {
            "id": "n0", "parent": None, "title": ing.title, "summary": "", "icon": "🧭", "depth": 0,
            "kind": "root", "sources": [c.id for c in ing.chunks], "sub": ing.outline,
            "status": "pending", "content": None, "children": [], "visited": False, "qa": [],
        }
        _write_json(path / "tree.json", {"root": "n0", "counter": 0, "nodes": {"n0": root}})
        return self.get(deck_id)

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.root.iterdir(), reverse=True):
            if not (p / "deck.json").exists():
                continue
            try:
                deck = self.get(p.name)
                out.append({**deck.meta, "progress": deck.progress()})
            except Exception:
                continue
        return out

    def delete(self, deck_id: str) -> bool:
        deck = self.get(deck_id)
        if not deck:
            return False
        with self._lock:
            self._decks.pop(deck_id, None)
        shutil.rmtree(deck.path, ignore_errors=True)
        return True
