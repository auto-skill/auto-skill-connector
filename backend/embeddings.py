"""Local gte-small embeddings, bit-compatible with Supabase Edge Functions.

Uses the exact ONNX weights the Supabase Edge runtime runs
(huggingface.co/Supabase/gte-small, onnx/model_quantized.onnx) so document
embeddings written by this machine and query embeddings computed at the edge
live in the same vector space. Mean-pooled over the attention mask, then
L2-normalized, matching session.run(text, {mean_pool: true, normalize: true}).
"""
import hashlib
import json
import re
import threading
from pathlib import Path

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
    parts = [skill.get("name") or ""]
    if skill.get("description"):
        parts.append(skill["description"])
    tags = skill.get("tags") or []
    if tags:
        parts.append(" ".join(str(t) for t in tags))
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
