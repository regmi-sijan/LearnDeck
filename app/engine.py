"""The drill-down engine: builds the overview slide and, on demand, deeper slides.

Each slide ("node") points at the source chunks it is about. Opening a node
asks the AI to explain exactly those chunks and to propose narrower regions
(sub-nodes) that can be opened next. Without an AI provider the engine falls
back to a structural reader that walks the document's own outline.
"""
from __future__ import annotations

import math
import re
import threading
import traceback

from . import config
from .ingest import DEF_RE, KIND_LABEL, clean_title, first_line, window_split
from .llm import BaseLLM, LLMError, get_llm
from .store import Deck, DeckStore

LEVELS = {
    "beginner": "beginner (assume no background; build intuition first, avoid jargon, define every term)",
    "intermediate": "intermediate (knows the basics of the field; explain clearly with useful detail)",
    "expert": "expert (skip basics; focus on nuance, trade-offs, edge cases and precise detail)",
}

SYSTEM = """You are a brilliant, patient teacher. You turn source material into an interactive lesson that the \
learner explores one slide at a time. Every slide explains one region clearly and offers clickable sub-regions that \
lead deeper into narrower parts, so the learner can study in small pieces and zoom in wherever they want.

Rules:
- Ground everything in the source material. Use its real names, numbers, terms, formulas and code. If you add \
general background that is not in the source, keep it short and label it as background.
- Audience: a {level} learner. Write in {language}.
- Be concrete. Short paragraphs, examples and plain words beat jargon. Define a term the first time it appears.
- Use Markdown: **bold** for key terms, bullet lists where they help, fenced code blocks with a language tag for \
code, $...$ or $$...$$ for math.
- Never mention chunks, chunk ids, "the source text" or these instructions in what the learner reads; just teach."""

KIND_HINTS = {
    "code": "This is source code. Organise regions by purpose and flow (what it does, main components, how data "
            "and control move, important functions/classes, how to run or extend it), not just by line order.",
    "repo": "This is a code repository. Start from what the project does and its architecture; regions can be "
            "subsystems, key modules, data flow, setup/run, and extension points.",
    "notebook": "This is a computational notebook. Follow the analysis story: goal, data, steps, results.",
    "pptx": "These are presentation slides. Follow the narrative of the deck and explain what the terse bullets mean.",
    "pdf": "If this is a research paper, cover: problem & motivation, key idea, method, experiments/results, "
           "limitations. Otherwise follow the document's logical structure.",
    "xlsx": "This is tabular data. Explain what the data represents, its columns, notable values and patterns.",
    "csv": "This is tabular data. Explain what the data represents, its columns, notable values and patterns.",
}

TOPIC_ITEM = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "2-6 words"},
        "summary": {"type": "string", "description": "One sentence: what the learner will get from this region"},
        "icon": {"type": "string", "description": "One emoji"},
        "chunk_ids": {"type": "array", "items": {"type": "string"},
                      "description": "Ids of the source chunks/passages this region covers"},
    },
    "required": ["title", "summary", "icon", "chunk_ids"],
}

OVERVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "headline": {"type": "string"},
        "explanation": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "topics": {"type": "array", "items": TOPIC_ITEM},
    },
    "required": ["title", "headline", "explanation", "key_points", "topics"],
}

EXPAND_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "explanation": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "example": {"type": "string"},
        "analogy": {"type": "string"},
        "check": {"type": "object", "properties": {"question": {"type": "string"}, "answer": {"type": "string"}},
                  "required": ["question", "answer"]},
        "subtopics": {"type": "array", "items": TOPIC_ITEM},
    },
    "required": ["headline", "explanation", "key_points", "example", "analogy", "check", "subtopics"],
}

OVERVIEW_PROMPT = """Build the OVERVIEW slide — the first thing the learner sees for this material ({kind}).
{hint}

Return:
- title: a short, clear name for the material (max ~8 words)
- headline: one sentence saying what this is and why it matters
- explanation: 120-220 words: what the material is, its big idea, and how it is organised; end with how to approach it
- key_points: 3-6 one-line takeaways for the whole material
- topics: {n_topics} regions for the learner to click, in the best order to learn them. Together they must cover the \
whole material without overlapping much. Each region: title, summary, icon (one emoji), chunk_ids (ids that appear in \
the source, e.g. "c3")."""

EXPAND_PROMPT = """The learner is exploring: {path}
They opened the region "{title}" — {summary}
{selection}
The previous slide already said (don't repeat it): {parent_brief}
{siblings}
FOCUS SOURCE for this region:
{focus}

Build the slide for this region. Return:
- headline: one sentence capturing the essence of this region
- explanation: 150-350 words that go one level deeper than the previous slide: what it is, why it matters, how it \
works. Use specifics from the focus source (names, numbers, steps, code).
- key_points: 2-6 one-line takeaways
- example: a concrete worked example, a real snippet with a short line-by-line explanation, or a step-by-step \
walk-through drawn from the source; '' if it would add nothing
- analogy: one or two sentences comparing it to something familiar; '' if not helpful
- check: a quick self-test question with a short answer
- subtopics: {sub_rule}"""

ASK_PROMPT = """The learner is on the slide "{title}" (path: {path}) and asks:
{question}

What the slide says: {brief}

FOCUS SOURCE for this slide:
{focus}

Answer in 60-250 words of Markdown, grounded in the source. If the source does not cover it, say so and give a brief \
general answer clearly labelled as background."""

VISION_PROMPT = """Transcribe all readable text in this image faithfully, keeping headings, bullet structure, \
tables (as Markdown tables), equations (as LaTeX) and code (as code blocks). Then add a section 'Visual description:' \
that briefly explains any diagrams, charts, figures or photos and what they show. Output Markdown only."""

ICONS = {"pdf": "📄", "pptx": "🖼️", "docx": "📝", "xlsx": "📊", "csv": "📊", "code": "🧩", "repo": "🗂️",
         "notebook": "📓", "markdown": "📝", "text": "📝", "web": "🌐", "image": "🖼️", "collection": "📚"}


class Engine:
    def __init__(self, store: DeckStore):
        self.store = store
        self._jobs: dict[tuple[str, str], threading.Thread] = {}
        self._jobs_lock = threading.Lock()

    # ------------------------------------------------------------------ jobs
    def ensure(self, deck: Deck, nid: str, retry: bool = False) -> None:
        """Start generating a node in the background if it isn't ready yet."""
        node = deck.node(nid)
        if node is None:
            return
        if node["status"] == "ready" or (node["status"] == "error" and not retry):
            return
        key = (deck.id, node["id"])
        with self._jobs_lock:
            job = self._jobs.get(key)
            if job and job.is_alive():
                return
            with deck.lock:
                node["status"], node["error"], node["progress"] = "working", None, "Thinking…"
            job = threading.Thread(target=self._run, args=(deck, node["id"]), daemon=True)
            self._jobs[key] = job
            job.start()

    def _run(self, deck: Deck, nid: str) -> None:
        node = deck.node(nid)
        try:
            self.expand(deck, node)
            with deck.lock:
                node["status"], node["progress"] = "ready", None
        except LLMError as e:
            with deck.lock:
                node["status"], node["error"] = "error", str(e)
        except Exception as e:  # keep the server alive, show the error on the slide
            traceback.print_exc()
            with deck.lock:
                node["status"], node["error"] = "error", f"Unexpected error: {e}"
        finally:
            deck.save_tree()
        if node["status"] == "ready" and config.PREFETCH_CHILDREN > 0 and get_llm().available:
            for child_id in node["children"][: config.PREFETCH_CHILDREN]:
                self.ensure(deck, child_id)

    # -------------------------------------------------------------- building
    def expand(self, deck: Deck, node: dict) -> None:
        llm = get_llm()
        if node["kind"] == "root":
            self._prepare_images(deck, node, llm)
        if llm.available:
            node["provider"] = f"{llm.name}:{llm.model}"
            if node["kind"] == "root":
                self._ai_overview(deck, node, llm)
            else:
                self._ai_expand(deck, node, llm)
        else:
            node["provider"] = "simple"
            self._simple_expand(deck, node)

    def _new_child(self, deck: Deck, parent: dict, *, title: str, summary: str, icon: str,
                   sources: list[str], kind: str = "topic", sub: list | None = None,
                   selection: str | None = None) -> dict:
        nid = deck.new_node_id()
        child = {
            "id": nid, "parent": parent["id"], "title": clean_title(title, 80) or "Untitled",
            "summary": (summary or "").strip(), "icon": (icon or "•").strip()[:4], "depth": parent["depth"] + 1,
            "kind": kind, "sources": sources, "sub": sub or [], "status": "pending", "content": None,
            "children": [], "visited": False, "qa": [],
        }
        if selection:
            child["selection"] = selection
        with deck.lock:
            deck.tree["nodes"][nid] = child
            parent["children"].append(nid)
        return child

    def _valid_ids(self, deck: Deck, ids, fallback: list[str]) -> list[str]:
        out = []
        for cid in ids or []:
            cid = str(cid).strip()
            if cid in deck.chunks and cid not in out:
                out.append(cid)
        return out or list(fallback)

    # ---------------------------------------------------------------- images
    def _prepare_images(self, deck: Deck, node: dict, llm: BaseLLM) -> None:
        pending = [c for c in deck.chunks.values() if c.get("vision") and not c.get("vision_done")]
        if not pending:
            return
        if not (llm.available and llm.supports_vision):
            for c in pending:
                if not c["text"].strip():
                    c["text"] = "[Image-only content — connect an AI provider in Settings to read it.]"
            deck.save_doc()
            return
        for k, c in enumerate(pending[: config.VISION_PAGE_LIMIT], start=1):
            node["progress"] = f"Reading images and scanned pages ({k}/{min(len(pending), config.VISION_PAGE_LIMIT)})…"
            img = self.chunk_image(deck, c)
            if not img:
                continue
            try:
                text = llm.vision(img[0], img[1], VISION_PROMPT)
            except LLMError as e:
                text = f"[Could not read this image: {e}]"
            c["text"] = (c["text"] + "\n\n" + text).strip()
            c["vision_done"] = True
        deck.save_doc()
        node["progress"] = "Thinking…"

    def chunk_image(self, deck: Deck, chunk: dict) -> tuple[bytes, str] | None:
        if chunk.get("image"):
            f = deck.image_file(chunk["image"])
            if f.exists():
                ext = f.suffix.lower().lstrip(".")
                return f.read_bytes(), {"jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, f"image/{ext}")
        if chunk.get("preview") and chunk.get("page"):
            png = self.render_page(deck, chunk["preview"], int(chunk["page"]))
            if png:
                return png, "image/png"
        return None

    def render_page(self, deck: Deck, key: str, page: int, zoom: float = 1.6) -> bytes | None:
        cache = deck.path / "previews" / f"{key}-p{page}.png"
        if cache.exists():
            return cache.read_bytes()
        pdf = deck.preview_file(key)
        if not pdf.exists():
            return None
        from .ingest import _pymupdf

        pymupdf = _pymupdf()
        with pymupdf.open(str(pdf)) as doc:
            if not 1 <= page <= doc.page_count:
                return None
            pix = doc[page - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            png = pix.tobytes("png")
        cache.write_bytes(png)
        return png

    # ---------------------------------------------------------- source text
    def _chunk_xml(self, c: dict, limit: int | None = None) -> str:
        text = c["text"] if limit is None or len(c["text"]) <= limit else c["text"][:limit] + " …[truncated]"
        attrs = f'id="{c["id"]}"' + (f' loc="{c["loc"]}"' if c.get("loc") else "")
        return f'<chunk {attrs} title="{c["title"]}">\n{text}\n</chunk>'

    def _context(self, deck: Deck, llm: BaseLLM, full_ok: bool) -> str:
        """Stable document text that providers can cache between requests."""
        meta = deck.meta
        order = [deck.chunks[i] for i in deck.doc["order"] if i in deck.chunks]
        total = sum(len(c["text"]) for c in order)
        head = f'<document title="{meta["title"]}" type="{KIND_LABEL.get(meta["kind"], meta["kind"])}">\n'
        if full_ok and total <= llm.context_chars:
            body = "\n".join(self._chunk_xml(c) for c in order)
            return head + body + "\n</document>"
        if full_ok:  # condensed: every chunk, proportionally shortened
            budget = int(llm.context_chars * 0.9)
            each = max(160, budget // max(1, len(order)))
            body = "\n".join(self._chunk_xml(c, each) for c in order)
            if len(body) > llm.context_chars:
                body = body[: llm.context_chars] + "\n…[remaining chunks omitted]"
            return head + body + "\n</document>"
        # outline only: ids, locations and first lines (orientation for deep slides of big documents)
        lines = [f'{c["id"]} · {c.get("loc") or ""} · {c["title"]}' for c in order]
        body = "\n".join(lines)
        cap = min(40_000, llm.context_chars // 3)
        if len(body) > cap:
            body = body[:cap] + "\n…"
        return head + "<outline>\n" + body + "\n</outline>\n</document>"

    def _split_passages(self, deck: Deck, c: dict) -> list[dict]:
        """Cut a long chunk into finer passages (cached) so deeper slides can point at smaller pieces."""
        if c.get("passages"):
            return [deck.chunks[p] for p in c["passages"] if p in deck.chunks]
        text = c["text"]
        pieces: list[tuple[str, str]] = []
        is_code = bool(c.get("lang")) and not text.lstrip().startswith(("#", "```")) and "\n\n```" not in text
        if is_code:
            lines = text.split("\n")
            m = re.search(r"lines (\d+)", c.get("loc") or "")
            base = int(m.group(1)) if m else 1
            for s, e in _code_ranges(lines):
                pieces.append(("\n".join(lines[s:e]), f"lines {base + s}–{base + e - 1}"))
        else:
            for k, p in enumerate(window_split(text, 1800), start=1):
                pieces.append((p, f"part {k}"))
        if len(pieces) <= 1:
            return [c]
        ids = []
        with deck.lock:
            for k, (ptext, label) in enumerate(pieces, start=1):
                pid = f'{c["id"]}.{k}'
                loc = re.sub(r"lines \d+–\d+", label, c.get("loc") or "") if is_code and "lines" in (c.get("loc") or "") \
                    else f'{c.get("loc") or c["title"]}, {label}'
                deck.chunks[pid] = {**{k2: v for k2, v in c.items() if k2 != "passages"}, "id": pid,
                                    "title": first_line(ptext, 70) or f'{c["title"]} ({label})', "text": ptext,
                                    "loc": loc, "parent": c["id"]}
                ids.append(pid)
            c["passages"] = ids
        deck.save_doc()
        return [deck.chunks[p] for p in ids]

    def _focus(self, deck: Deck, ids: list[str], limit: int | None = None) -> tuple[str, list[dict]]:
        limit = limit or config.FOCUS_CHARS
        chunks = [deck.chunks[i] for i in ids if i in deck.chunks]
        units: list[dict] = []
        if len(chunks) <= 4:
            for c in chunks:
                units.extend(self._split_passages(deck, c) if len(c["text"]) > config.PASSAGE_SPLIT_CHARS else [c])
        else:
            units = chunks
        total = sum(len(u["text"]) for u in units) or 1
        each = None if total <= limit else max(300, limit // max(1, len(units)))
        return "\n".join(self._chunk_xml(u, each) for u in units), units

    def _system(self, deck: Deck) -> str:
        return SYSTEM.format(level=LEVELS.get(deck.meta.get("level"), LEVELS["intermediate"]),
                             language=config.LANGUAGE)

    # ------------------------------------------------------------- AI slides
    def _ai_overview(self, deck: Deck, node: dict, llm: BaseLLM) -> None:
        n = len(deck.doc["order"])
        n_topics = "2-5" if n <= 3 else ("3-7" if n <= 12 else "4-8")
        kind = deck.meta["kind"]
        prompt = OVERVIEW_PROMPT.format(kind=KIND_LABEL.get(kind, kind), hint=KIND_HINTS.get(kind, ""),
                                        n_topics=n_topics)
        node["progress"] = "Reading the whole document and planning the lesson…"
        data = llm.json(system=self._system(deck), context=self._context(deck, llm, True), prompt=prompt,
                        schema=OVERVIEW_SCHEMA, max_tokens=4096)
        content = {
            "headline": str(data.get("headline", "")).strip(),
            "explanation": str(data.get("explanation", "")).strip(),
            "key_points": [str(x) for x in data.get("key_points", [])][:8],
            "example": "", "analogy": "", "check": None,
        }
        title = clean_title(str(data.get("title") or ""), 90)
        with deck.lock:
            if title:
                node["title"] = title
                deck.meta["title"] = title
            node["content"] = content
            node["children"] = []
        deck.save_meta()
        for t in (data.get("topics") or [])[:9]:
            self._new_child(deck, node, title=t.get("title", ""), summary=t.get("summary", ""),
                            icon=t.get("icon", "•"), sources=self._valid_ids(deck, t.get("chunk_ids"), node["sources"]))

    def _ai_expand(self, deck: Deck, node: dict, llm: BaseLLM) -> None:
        path = self.breadcrumb(deck, node["id"])
        parent = deck.node(node["parent"])
        pc = parent.get("content") or {}
        parent_brief = (pc.get("headline", "") + " " + "; ".join(pc.get("key_points", [])[:4])).strip() or "—"
        sibs = [deck.node(s)["title"] for s in parent["children"] if s != node["id"] and deck.node(s)]
        siblings = f"Neighbouring regions covered on their own slides: {', '.join(sibs[:10])}\n" if sibs else ""
        node["progress"] = "Reading the relevant part…"
        focus, units = self._focus(deck, node["sources"])
        depth_left = config.MAX_DEPTH - node["depth"]
        if depth_left > 0:
            sub_rule = ("0-6 narrower regions INSIDE this region that deserve their own deeper slide (concepts, "
                        "steps, components, functions, formulas, arguments …). Each: title, summary, icon, chunk_ids "
                        f"chosen from the focus source ids ({', '.join(u['id'] for u in units[:60])}); pick the "
                        "finest ids that fit. Return [] if this region is already fully explained by this slide.")
        else:
            sub_rule = "return an empty list [] (this is the deepest level)."
        selection = ""
        if node.get("selection"):
            selection = ("On the previous slide the learner highlighted this passage and asked to go deeper:\n«"
                         + node["selection"][:1500] + "»\nMake this slide explain exactly that, in context.\n")
        prompt = EXPAND_PROMPT.format(path=" › ".join(path), title=node["title"], summary=node["summary"] or "",
                                      selection=selection, parent_brief=parent_brief[:800], siblings=siblings,
                                      focus=focus, sub_rule=sub_rule)
        node["progress"] = "Writing the explanation…"
        data = llm.json(system=self._system(deck), context=self._context(deck, llm, False), prompt=prompt,
                        schema=EXPAND_SCHEMA, max_tokens=4096)
        check = data.get("check") if isinstance(data.get("check"), dict) else None
        if check and not str(check.get("question", "")).strip():
            check = None
        content = {
            "headline": str(data.get("headline", "")).strip(),
            "explanation": str(data.get("explanation", "")).strip(),
            "key_points": [str(x) for x in data.get("key_points", [])][:8],
            "example": str(data.get("example") or "").strip(),
            "analogy": str(data.get("analogy") or "").strip(),
            "check": check,
        }
        with deck.lock:
            node["content"] = content
            node["children"] = [c for c in node["children"] if deck.node(c) and deck.node(c)["kind"] == "dive"]
        if depth_left > 0:
            unit_ids = [u["id"] for u in units]
            for t in (data.get("subtopics") or [])[:7]:
                self._new_child(deck, node, title=t.get("title", ""), summary=t.get("summary", ""),
                                icon=t.get("icon", "•"), sources=self._valid_ids(deck, t.get("chunk_ids"), unit_ids))

    # ------------------------------------------------- simple (no AI) slides
    def _simple_expand(self, deck: Deck, node: dict) -> None:
        kind = deck.meta["kind"]
        chunks = [deck.chunks[i] for i in node["sources"] if i in deck.chunks]
        note = ("*Simple mode: showing the material's own structure. Add an AI key in **Settings** for "
                "explanations, examples and quizzes.*")
        if node["kind"] == "root":
            words = sum(len(c["text"].split()) for c in chunks)
            minutes = max(1, math.ceil(words / 230))
            first = next((c for c in chunks if len(c["text"]) > 200), chunks[0] if chunks else {"text": ""})
            intro = first["text"]
            node["content"] = {
                "headline": f"{KIND_LABEL.get(kind, kind)}, {len(chunks)} part{'s' if len(chunks) != 1 else ''}, about "
                            f"{minutes} minute{'s' if minutes != 1 else ''} to read",
                "explanation": note + "\n\n" + _preview(intro, 900, first.get("lang")),
                "key_points": [], "example": "", "analogy": "", "check": None,
            }
            groups = node.get("sub") or _auto_groups(deck, node["sources"])
        else:
            if len(chunks) == 1:
                c = chunks[0]
                units = self._split_passages(deck, c) if len(c["text"]) > config.PASSAGE_SPLIT_CHARS else [c]
                body = _preview(c["text"], 14000, c.get("lang"))
                node["content"] = {"headline": c.get("loc") or c["title"], "explanation": body,
                                   "key_points": _key_lines(c["text"]) if not c.get("lang") else [],
                                   "example": "", "analogy": "", "check": None}
                groups = [{"title": u["title"], "chunk_ids": [u["id"]], "children": []} for u in units] \
                    if len(units) > 1 and node["depth"] < config.MAX_DEPTH else []
            else:
                first = chunks[0]
                node["content"] = {
                    "headline": f"{len(chunks)} parts, from {chunks[0].get('loc', '')} to {chunks[-1].get('loc', '')}",
                    "explanation": _preview(first["text"], 700, first.get("lang")),
                    "key_points": [c["title"] for c in chunks[:6]], "example": "", "analogy": "", "check": None,
                }
                groups = node.get("sub") or _auto_groups(deck, node["sources"])
        with deck.lock:
            node["children"] = [c for c in node["children"] if deck.node(c) and deck.node(c)["kind"] == "dive"]
        for g in groups:
            ids = [i for i in g["chunk_ids"] if i in deck.chunks]
            if not ids:
                continue
            first = deck.chunks[ids[0]]
            self._new_child(deck, node, title=g["title"],
                            summary=first_line(first["text"], 120) if len(ids) == 1 else f"{len(ids)} parts",
                            icon=ICONS.get(kind, "•"), sources=ids, sub=g.get("children") or [])

    # ------------------------------------------------------------ user acts
    def dive(self, deck: Deck, parent_id: str, selection: str) -> dict:
        parent = deck.node(parent_id)
        selection = re.sub(r"\s+", " ", selection).strip()[:2000]
        title = "“" + clean_title(selection, 48) + "”"
        child = self._new_child(deck, parent, title=title, summary="A closer look at the passage you highlighted.",
                                icon="🔍", sources=list(parent["sources"]), kind="dive", selection=selection)
        deck.save_tree()
        if not get_llm().available:  # simple mode: find where the passage comes from
            hits = _search(deck, parent["sources"], selection, 3)
            if hits:
                child["sources"] = hits
        self.ensure(deck, child["id"])
        return child

    def ask(self, deck: Deck, nid: str, question: str) -> str:
        node = deck.node(nid)
        llm = get_llm()
        question = question.strip()[:2000]
        if llm.available:
            focus, _ = self._focus(deck, node["sources"], limit=40_000)
            c = node.get("content") or {}
            prompt = ASK_PROMPT.format(title=node["title"], path=" › ".join(self.breadcrumb(deck, nid)),
                                       question=question, brief=(c.get("headline", "") + " "
                                                                 + c.get("explanation", "")[:1500]), focus=focus)
            answer = llm.text(system=self._system(deck), context=self._context(deck, llm, False), prompt=prompt,
                              max_tokens=1200)
        else:
            hits = _search(deck, node["sources"] or deck.doc["order"], question, 2)
            if hits:
                answer = "*Simple mode. These passages look most relevant:*\n\n" + "\n\n".join(
                    f"**{deck.chunks[h].get('loc') or deck.chunks[h]['title']}**\n\n> "
                    + _best_snippet(deck.chunks[h]["text"], question).replace("\n", "\n> ") for h in hits)
            else:
                answer = "I couldn't find that in this part. Add an AI key in Settings to ask free-form questions."
        with deck.lock:
            node.setdefault("qa", []).append({"q": question, "a": answer})
        deck.save_tree()
        return answer

    def regenerate(self, deck: Deck, nid: str) -> None:
        node = deck.node(nid)
        with deck.lock:
            def drop(cid):
                child = deck.tree["nodes"].pop(cid, None)
                for g in (child or {}).get("children", []):
                    drop(g)
            for cid in list(node["children"]):
                drop(cid)
            node["children"], node["content"], node["status"] = [], None, "pending"
        deck.save_tree()
        self.ensure(deck, nid)

    def breadcrumb(self, deck: Deck, nid: str) -> list[str]:
        return [n["title"] for n in deck.path_to(nid)]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _code_ranges(lines: list[str], target: int = 60) -> list[tuple[int, int]]:
    n = len(lines)
    if n <= int(target * 1.4):
        return [(0, n)]
    cuts = [0]
    while cuts[-1] + int(target * 1.4) < n:
        start = cuts[-1]
        lo, hi = start + int(target * 0.6), min(n - 1, start + int(target * 1.4))
        best = None
        for j in range(lo, hi + 1):
            s = lines[j].strip()
            if s and DEF_RE.match(s) and (j == 0 or not lines[j - 1].strip().startswith("@")):
                best = j
                break
        if best is None:
            for j in range(hi, lo - 1, -1):
                if not lines[j].strip():
                    best = j + 1
                    break
        cuts.append(best if best and best > start else start + target)
    return [(cuts[k], cuts[k + 1] if k + 1 < len(cuts) else n) for k in range(len(cuts))]


def _auto_groups(deck: Deck, ids: list[str], max_groups: int = 8) -> list[dict]:
    ids = [i for i in ids if i in deck.chunks]
    if len(ids) <= 10:
        return [{"title": deck.chunks[i]["title"], "chunk_ids": [i], "children": []} for i in ids]
    size = math.ceil(len(ids) / max_groups)
    out = []
    for k in range(0, len(ids), size):
        part = ids[k:k + size]
        a, b = deck.chunks[part[0]], deck.chunks[part[-1]]
        title = f"{a.get('loc') or a['title']} – {b.get('loc') or b['title']}"
        out.append({"title": title, "chunk_ids": part, "children": []})
    return out


def _preview(text: str, limit: int, lang: str | None) -> str:
    snippet = text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…"
    if lang and not snippet.lstrip().startswith(("```", "#")) and "\n```" not in snippet:
        return f"```{lang}\n{snippet}\n```"
    return snippet


def _key_lines(text: str) -> list[str]:
    bullets = [re.sub(r"^\s*[-•*]\s+", "", ln).strip() for ln in text.splitlines()
               if re.match(r"^\s*[-•*]\s+\S", ln)]
    if len(bullets) >= 2:
        return [clean_title(b, 140) for b in bullets[:5]]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) > 60]
    return [clean_title(re.split(r"(?<=[.!?])\s", p, 1)[0], 140) for p in paras[:4]]


def _words(s: str) -> set[str]:
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "it", "this", "that", "what", "how", "why",
            "for", "on", "with", "does", "do", "are", "be", "as", "by", "can", "i", "you", "me"}
    return {w for w in re.findall(r"[a-z0-9_]{3,}", s.lower()) if w not in stop}


def _search(deck: Deck, ids: list[str], query: str, k: int) -> list[str]:
    q = _words(query)
    if not q:
        return []
    scored = []
    for i in ids:
        c = deck.chunks.get(i)
        if not c:
            continue
        w = _words(c["text"])
        score = len(q & w) / (len(q) or 1)
        if score:
            scored.append((score, i))
    scored.sort(reverse=True)
    return [i for _, i in scored[:k]]


def _best_snippet(text: str, query: str, size: int = 500) -> str:
    q = _words(query)
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()] or [text]
    best = max(paras, key=lambda p: len(q & _words(p)))
    return best[:size] + ("…" if len(best) > size else "")
