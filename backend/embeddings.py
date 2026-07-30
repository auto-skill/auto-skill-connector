"""Local gte-small embeddings, bit-compatible with Supabase Edge Functions.

Uses the exact ONNX weights the Supabase Edge runtime runs
(huggingface.co/Supabase/gte-small, onnx/model_quantized.onnx) so document
embeddings written by this machine and query embeddings computed at the edge
live in the same vector space. Mean-pooled over the attention mask, then
L2-normalized, matching session.run(text, {mean_pool: true, normalize: true}).
"""
import hashlib
import json
import os
import re
import threading
from pathlib import Path

import httpx
import numpy as np
import onnxruntime
from huggingface_hub import hf_hub_download
from tokenizers import Tokenizer

MODEL_REPO = "Supabase/gte-small"
MODEL_FILE = "onnx/model_quantized.onnx"
MAX_TOKENS = 512
EMBED_DIM = 384

# gte-small's 512-token window is ~1,800-2,000 chars of English; text beyond
# that is truncated by the tokenizer anyway, so the final embed string stays
# within this ceiling. Content sampling (below) decides *which* body bytes
# fill the remaining budget after metadata — not a blind head clip.
MAX_EMBED_CHARS = 2000
# Soft content budget before metadata join; actual slice is min(this, leftover).
MAX_CONTENT_CHARS = 1800

_session = None
_tokenizer = None
_load_lock = threading.RLock()
_load_error = ""

_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.S)
_BADGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]{1,200}>")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WS_RE = re.compile(r"\s+")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
# Prefer operational guidance over long reference dumps when the window is tight.
_PRIORITY_HEADINGS = (
    "when to use",
    "workflow",
    "steps",
    "instructions",
    "constraints",
    "output",
    "verification",
    "examples",
)


def _load():
    global _session, _tokenizer, _load_error
    if _session is None:
        with _load_lock:
            if _session is None:
                try:
                    model_path = hf_hub_download(MODEL_REPO, MODEL_FILE)
                    tokenizer_path = hf_hub_download(MODEL_REPO, "tokenizer.json")
                    _session = onnxruntime.InferenceSession(model_path, providers=["CPUExecutionProvider"])
                    _tokenizer = Tokenizer.from_file(tokenizer_path)
                    _tokenizer.enable_truncation(max_length=MAX_TOKENS)
                    _load_error = ""
                except Exception as exc:
                    _session = None
                    _tokenizer = None
                    _load_error = f"{type(exc).__name__}: {exc}"[:300]
                    raise
    return _session, _tokenizer


def embedding_model_status(*, warm: bool = False) -> dict[str, object]:
    """Report whether the local ONNX runtime can serve semantic queries.

    ``warm=True`` is used by readiness checks. Docker preloads the model files,
    so this validates the actual tokenizer/session instead of treating a cache
    directory as proof that vector search will work.
    """
    if warm and _session is None:
        try:
            _load()
        except Exception:
            pass
    return {
        "ready": _session is not None and _tokenizer is not None,
        "error": _load_error or None,
    }


def embed_texts(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Embed a list of strings -> list of 384-dim L2-normalized vectors."""
    session, tokenizer = _load()
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = [t if t.strip() else " " for t in texts[i:i + batch_size]]
        encodings = tokenizer.encode_batch(batch)
        max_len = max(len(e.ids) for e in encodings)
        input_ids = np.zeros((len(batch), max_len), dtype=np.int64)
        attention_mask = np.zeros((len(batch), max_len), dtype=np.int64)
        token_type_ids = np.zeros((len(batch), max_len), dtype=np.int64)
        for j, enc in enumerate(encodings):
            input_ids[j, :len(enc.ids)] = enc.ids
            attention_mask[j, :len(enc.ids)] = enc.attention_mask
        (hidden,) = session.run(None, {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        })
        mask = attention_mask[:, :, None].astype(np.float32)
        pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        pooled = pooled / np.clip(norms, 1e-9, None)
        out.extend(pooled.astype(np.float32).tolist())
    return out


def clean_markdown(text: str) -> str:
    text = _FRONTMATTER_RE.sub(" ", text)
    text = _BADGE_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _LINK_RE.sub(r"\1", text)  # keep link text, drop URLs
    return _WS_RE.sub(" ", text).strip()


def _body_sections(content: str) -> tuple[str, list[tuple[str, str]]]:
    """Split SKILL.md into (preamble, [(heading, body), ...]) after frontmatter."""
    body = _FRONTMATTER_RE.sub("", content or "", count=1).strip()
    matches = list(_HEADING_RE.finditer(body))
    if not matches:
        return body, []
    preamble = body[: matches[0].start()].strip()
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        heading = re.sub(r"\s+", " ", match.group(2)).strip()
        section_body = body[start:end].strip()
        if heading or section_body:
            sections.append((heading, section_body))
    return preamble, sections


def _priority_rank(heading: str) -> int:
    lower = heading.casefold()
    for index, key in enumerate(_PRIORITY_HEADINGS):
        if key in lower:
            return index
    return len(_PRIORITY_HEADINGS) + 1


def _append_budget(parts: list[str], chunk: str, budget: int) -> int:
    """Append cleaned ``chunk`` while budget remains; return leftover budget."""
    cleaned = clean_markdown(chunk)
    if not cleaned or budget <= 0:
        return budget
    if len(cleaned) > budget:
        cleaned = cleaned[:budget].rstrip()
    if not cleaned:
        return budget
    parts.append(cleaned)
    return budget - len(cleaned)


def sample_content_for_embed(content: str, budget: int = MAX_CONTENT_CHARS) -> str:
    """Select embed body text that reflects full-skill substance within ``budget``.

    Strategy (documented product choice for the gte-small window):
      1. Drop YAML frontmatter (name/description already live in metadata parts).
      2. Keep a short head/preamble so opening guidance is always represented.
      3. Prefer priority sections (when to use, workflow, steps, instructions,
         constraints, output, verification, examples) in that order.
      4. Keep a short tail from the final section so closing notes are not invisible.
      5. Fill any remainder with other sections in document order.

    This is intentionally not ``content[:budget]``: a head-only clip ignores
    mid/late operational sections that often carry the real skill substance.
    Canonical storage remains the complete SKILL.md; this only chooses what
    enters the 512-token embedding window.
    """
    budget = max(0, int(budget or 0))
    if not content or budget <= 0:
        return ""
    preamble, sections = _body_sections(content)
    if not sections:
        return clean_markdown(preamble or content)[:budget]

    parts: list[str] = []
    # Head: ~25% of budget, minimum 120 when budget allows.
    head_budget = min(budget, max(120, budget // 4)) if budget >= 120 else budget
    remaining = _append_budget(parts, preamble, head_budget)
    remaining += budget - head_budget

    # Tail reservation from the last section (~20%), filled after priorities.
    tail_reserve = min(remaining, max(80, budget // 5)) if remaining >= 80 and len(sections) > 1 else 0
    work_budget = remaining - tail_reserve

    ordered = sorted(
        enumerate(sections),
        key=lambda item: (_priority_rank(item[1][0]), item[0]),
    )
    used_indexes: set[int] = set()
    for index, (heading, body) in ordered:
        if work_budget <= 0:
            break
        chunk = f"{heading}. {body}" if heading else body
        before = work_budget
        work_budget = _append_budget(parts, chunk, work_budget)
        if work_budget < before:
            used_indexes.add(index)

    remaining = work_budget + tail_reserve
    if remaining > 0 and sections:
        last_index = len(sections) - 1
        if last_index not in used_indexes:
            heading, body = sections[last_index]
            chunk = f"{heading}. {body}" if heading else body
            # Prefer the end of a long final section so the true tail is visible.
            cleaned_tail = clean_markdown(chunk)
            if len(cleaned_tail) > remaining:
                chunk = cleaned_tail[-remaining:]
            remaining = _append_budget(parts, chunk, remaining)
            used_indexes.add(last_index)

    if remaining > 0:
        for index, (heading, body) in enumerate(sections):
            if index in used_indexes:
                continue
            if remaining <= 0:
                break
            chunk = f"{heading}. {body}" if heading else body
            remaining = _append_budget(parts, chunk, remaining)

    return _WS_RE.sub(" ", " ".join(parts)).strip()[:budget]


def build_embed_text(skill: dict, content: str = "") -> str:
    """The canonical text a skill is embedded from. Changing this invalidates
    all stored embeddings (embedding_text_hash catches that per row).

    capability_summary is an LLM-generated read of what the skill actually
    does (task, triggers, capabilities) and is deliberately placed ahead of
    the raw content -- it's a distilled signal of intent, where raw content
    is often front-loaded with badges/install instructions instead.

    Body text uses ``sample_content_for_embed`` so indexing reflects head +
    priority sections + tail within the model window, not a random head clip.
    """
    # Package-first ingestion creates a compact, versioned retrieval record.
    # Prefer it when available; legacy rows retain the content-aware sampler
    # below until they are rebuilt into the new representation.
    retrieval_text = str(skill.get("retrieval_text") or "").strip()
    if retrieval_text:
        return retrieval_text[:1500]

    parts = [skill.get("name") or ""]
    if skill.get("description"):
        parts.append(skill["description"])
    tags = skill.get("tags") or []
    if tags:
        parts.append(" ".join(str(t) for t in tags))
    triggers = skill.get("triggers") or []
    if triggers:
        parts.append(" ".join(str(t) for t in triggers))
    if skill.get("capability_summary"):
        parts.append(skill["capability_summary"])
    meta = _WS_RE.sub(" ", ". ".join(p for p in parts if p).strip())
    leftover = max(0, MAX_EMBED_CHARS - len(meta) - (2 if meta else 0))
    body_budget = min(MAX_CONTENT_CHARS, leftover)
    if content and body_budget > 0:
        sampled = sample_content_for_embed(content, body_budget)
        if sampled:
            parts.append(sampled)
    return _WS_RE.sub(" ", ". ".join(p for p in parts if p).strip())[:MAX_EMBED_CHARS]


def embed_text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
SUMMARY_MODEL = os.getenv("AUTOSKILL_SUMMARY_MODEL", "llama3.2:3b")
SUMMARY_MAX_CONTENT_CHARS = 4000
SUMMARY_MAX_CHARS = 2000

MAX_TRIGGERS = 6
MAX_TRIGGER_CHARS = 140

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "triggers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "triggers"],
}

SUMMARY_SYSTEM = (
    "You analyze developer tool/skill listings for a search index. Given a name, "
    "short description, and (if available) file content, write JSON: "
    "{\"summary\": \"...\", \"triggers\": [\"...\"]}. "
    "summary: 1-3 plain factual sentences stating what the skill/tool actually does "
    "and what task or workflow it helps with. Third person, no marketing language, no "
    "meta-commentary ('this skill', 'in summary'), no preamble. "
    "triggers: up to 6 short, concrete phrases describing situations where THIS SPECIFIC "
    "skill should be recommended -- the kind of user request or task that should match "
    "it. Every trigger must be derived only from the name/description/content given "
    "below in this request, in this skill's own domain -- never reuse or adapt phrasing "
    "from these instructions, and never produce a trigger about a different kind of tool "
    "than the one described. Phrase each trigger as the task/request itself (a gerund "
    "phrase, e.g. starting with a verb+ing), never as an instruction sentence directed at "
    "the skill (never start a trigger with 'use when' or 'when the user'). Deduplicate "
    "near-identical triggers. If the material is too sparse to say anything concrete, "
    "summarize plainly what little is known and return an empty triggers list instead of "
    "inventing detail."
)


async def generate_capability_summary(client: httpx.AsyncClient, name: str, description: str, content: str) -> dict:
    """One local-Ollama call distilling a skill's task/capabilities (summary)
    and the concrete situations that should match it (triggers) -- both are
    prioritized ahead of raw content in build_embed_text (see above), and
    triggers separately feed FTS/structured matching (see local_store.py).

    Returns {"summary": str, "triggers": list[str]}, both possibly empty.
    """
    description = (description or "").strip()
    body = (content or "")[:SUMMARY_MAX_CONTENT_CHARS].strip()
    empty = {"summary": "", "triggers": []}
    if not description and not body:
        return empty
    user = (
        f"Name: {name or ''}\n"
        f"Description: {description or '(none)'}\n"
        f"Content:\n{body or '(no file content available)'}"
    )
    for attempt in range(2):
        try:
            r = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": SUMMARY_MODEL,
                    "messages": [
                        {"role": "system", "content": SUMMARY_SYSTEM},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    "format": SUMMARY_SCHEMA,
                    "options": {"temperature": 0, "num_predict": 400},
                },
                timeout=60,
            )
            if r.status_code != 200:
                continue
            content_json = r.json().get("message", {}).get("content", "")
            parsed = json.loads(content_json)
            summary = str(parsed.get("summary") or "").strip()[:SUMMARY_MAX_CHARS]
            triggers = []
            for t in (parsed.get("triggers") or [])[:MAX_TRIGGERS]:
                t = str(t or "").strip()[:MAX_TRIGGER_CHARS]
                if t and t not in triggers:
                    triggers.append(t)
            return {"summary": summary, "triggers": triggers}
        except (json.JSONDecodeError, httpx.HTTPError):
            continue
    return empty


class LibraryContent:
    """url -> saved .md content lookup, backed by skills_library/index.json."""

    _index_cache: dict[str, tuple[int, dict[str, str], dict[str, str]]] = {}

    def __init__(self, library_dir: Path | None = None):
        self.library_dir = library_dir or Path(__file__).parent / "skills_library"
        self.files_dir = self.library_dir / "files"
        self._index: dict[str, str] = {}
        self._hash_index: dict[str, str] = {}
        index_path = self.library_dir / "index.json"
        if index_path.exists():
            try:
                cache_key = str(index_path.resolve())
                mtime_ns = index_path.stat().st_mtime_ns
                cached = self._index_cache.get(cache_key)
                if cached and cached[0] == mtime_ns:
                    self._index = cached[1]
                    self._hash_index = cached[2]
                    return
                raw = json.loads(index_path.read_text(encoding="utf-8"))
                self._index = {url: entry.get("file", "") for url, entry in raw.items() if entry.get("file")}
                self._hash_index = {
                    entry.get("content_hash", ""): entry.get("file", "")
                    for entry in raw.values()
                    if entry.get("content_hash") and entry.get("file")
                }
                self._index_cache[cache_key] = (mtime_ns, self._index, self._hash_index)
            except Exception:
                self._index = {}
                self._hash_index = {}

    def get(self, url: str) -> str:
        filename = self._index.get(url)
        if not filename:
            return ""
        try:
            return (self.files_dir / filename).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    def get_by_hash(self, hash_value: str) -> str:
        filename = self._hash_index.get(hash_value)
        if not filename:
            return ""
        try:
            return (self.files_dir / filename).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
