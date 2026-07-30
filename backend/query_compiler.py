"""Cheap, deterministic intent compilation for retrieval queries.

The compiler sees only the public task text and optional coarse client tags.
It never reads benchmark identifiers, candidate labels, skill bodies, or
verifiers.  Its output is deliberately inspectable so retrieval experiments
can attribute gains to query formulation rather than a hidden planner.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Iterable


COMPILER_VERSION = "structured-intent-v1"
MAX_QUERY_CHARS = 240
MAX_QUERY_WORDS = 32

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#._/-]*", re.I)
_SPACE_RE = re.compile(r"\s+")
_VERSION_RE = re.compile(r"\b(?:v(?:ersion)?\s*)?\d+(?:\.\d+){1,3}\b", re.I)
_FAILURE_RE = re.compile(
    r"\b(?:error|exception|fail(?:ed|ing|ure)?|bug|crash(?:ed|ing)?|timeout|"
    r"hang(?:ing)?|broken|invalid|corrupt(?:ed|ion)?|cannot|can't|unable|"
    r"doesn'?t|won'?t|deprecated|removed|incompatible|race condition)\b",
    re.I,
)

_STOPWORDS = frozenset(
    "a an and are as at be been being by can could do does for from get give have help how i in into is it its make me my of on or our please should show that the their them then this to turn use using want we what when where which while with would you your".split()
)

_TECHNOLOGIES = (
    "active directory", "amazon web services", "android", "ansible", "apache",
    "asyncio", "aws", "azure", "bash", "c++", "c#", "caffe", "chrome",
    "chromium", "cloudflare", "cobol", "cuda", "cython", "debian", "django",
    "docker", "elasticsearch", "excel", "fastapi", "ffmpeg", "firebase",
    "flask", "gcp", "git", "github", "gitlab", "go", "golang", "google sheets",
    "grafana", "grpc", "html", "java", "javascript", "jira", "kubernetes",
    "latex", "linux", "mongodb", "mysql", "next.js", "nginx", "node.js",
    "notion", "numpy", "ocaml", "ollama", "openssl", "oracle", "pandas",
    "pdf", "php", "playwright", "postgres", "postgresql", "powerpoint", "powershell", "python",
    "prometheus", "puppeteer", "pytorch", "qemu", "r", "react", "redis",
    "ruby", "rust", "salesforce", "sentry", "shopify", "slack", "spark", "sqlite",
    "ssh", "stripe", "supabase", "tensorflow", "terraform", "typescript",
    "ubuntu", "vue", "windows", "wordpress",
)

_OPERATION_RULES = (
    ("debug repair", ("debug", "fix", "repair", "diagnose", "troubleshoot")),
    ("build compile", ("build", "compile", "install from source")),
    ("create implement build", ("create", "implement", "write", "add", "develop", "build")),
    ("convert migrate", ("convert", "migrate", "modernize", "port")),
    ("extract parse", ("extract", "parse", "recover", "decode")),
    ("configure deploy", ("configure", "deploy", "publish", "provision")),
    ("test verify", ("test", "verify", "validate", "check")),
    ("analyze optimize", ("analyze", "optimise", "optimize", "tune", "profile")),
    ("search retrieve", ("search", "retrieve", "find", "look up")),
    ("merge synchronize", ("merge", "sync", "synchronize", "combine")),
    ("monitor summarize", ("monitor", "summarize", "track")),
    ("review audit", ("review", "audit", "inspect")),
)

_ARTIFACT_RULES = (
    ("api endpoint", ("api", "endpoint", "webhook")),
    ("command line tool", ("cli", "command line", "executable")),
    ("database", ("database", "db", "table", "query", "schema")),
    ("spreadsheet", ("spreadsheet", "workbook", "xlsx", "worksheet", "excel")),
    ("document", ("document", "docx", "report")),
    ("presentation", ("presentation", "slides", "slide deck", "pptx")),
    ("pdf", ("pdf", "pdf form")),
    ("web page", ("web page", "webpage", "website", "landing page", "html")),
    ("image", ("image", "screenshot", "photo")),
    ("video", ("video", "recording")),
    ("audio transcript", ("audio", "transcript", "speech")),
    ("source code", ("source code", "codebase", "repository", "repo", "function", "class", "module")),
    ("test suite", ("test suite", "tests", "verifier")),
    ("model", ("model", "checkpoint", "weights", "inference")),
    ("dataset", ("dataset", "csv", "jsonl", "parquet")),
    ("certificate", ("certificate", "cert", "tls", "ssl")),
)

_CONSTRAINT_MARKERS = (
    "without", "must", "preserve", "exactly", "only", "under", "at most",
    "at least", "compatible", "compatibility", "offline", "cpu-only",
    "read-only", "deterministic", "byte-identical", "bounded", "required",
)


@dataclass(frozen=True)
class CompiledIntent:
    original_query: str
    compressed_query: str
    technology: tuple[str, ...]
    operation: tuple[str, ...]
    artifact: tuple[str, ...]
    constraints: tuple[str, ...]
    failure_mode: tuple[str, ...]
    query_variants: tuple[str, ...]
    compiler_version: str = COMPILER_VERSION

    def as_dict(self) -> dict:
        return asdict(self)


def _normalise(text: str) -> str:
    return _SPACE_RE.sub(" ", (text or "").replace("\x00", " ")).strip()


def _contains(text: str, phrase: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text))


def _matched_values(text: str, rules: Iterable[tuple[str, Iterable[str]]]) -> tuple[str, ...]:
    values: list[str] = []
    for canonical, markers in rules:
        if any(_contains(text, marker) for marker in markers):
            values.append(canonical)
    return tuple(values[:4])


def _constraint_phrases(text: str) -> tuple[str, ...]:
    sentences = re.split(r"(?<=[.!?;])\s+|\n+", text)
    found: list[str] = []
    for sentence in sentences:
        lowered = sentence.casefold()
        if any(marker in lowered for marker in _CONSTRAINT_MARKERS) or _VERSION_RE.search(sentence):
            tokens = [token.casefold() for token in _TOKEN_RE.findall(sentence)]
            useful = [token for token in tokens if token not in _STOPWORDS]
            if useful:
                found.append(" ".join(useful[:8]))
    return tuple(dict.fromkeys(found))[:3]


def _failure_phrases(text: str) -> tuple[str, ...]:
    tokens = list(_TOKEN_RE.finditer(text))
    found: list[str] = []
    for match in _FAILURE_RE.finditer(text):
        token_index = next((index for index, token in enumerate(tokens) if token.start() >= match.start()), 0)
        start = max(0, token_index - 3)
        window = [token.group(0).casefold() for token in tokens[start : token_index + 5]]
        useful = [token for token in window if token not in _STOPWORDS]
        if useful:
            found.append(" ".join(useful))
    return tuple(dict.fromkeys(found))[:2]


def _fallback_keywords(text: str) -> list[str]:
    values: list[str] = []
    for token in _TOKEN_RE.findall(text.casefold()):
        token = token.strip("./-")
        if len(token) < 3 or token in _STOPWORDS or token.isdigit():
            continue
        if token not in values:
            values.append(token)
        if len(values) >= 14:
            break
    return values


def _bounded_query(parts: Iterable[str]) -> str:
    words: list[str] = []
    for part in parts:
        for word in _SPACE_RE.split(part.strip()):
            if word and word not in words:
                words.append(word)
            if len(words) >= MAX_QUERY_WORDS:
                break
        if len(words) >= MAX_QUERY_WORDS:
            break
    result = " ".join(words)
    return result[:MAX_QUERY_CHARS].rstrip(" ,.;:-")


def compile_intent_query(
    task: str,
    *,
    languages: Iterable[str] = (),
    frameworks: Iterable[str] = (),
    project_tags: Iterable[str] = (),
) -> CompiledIntent:
    """Compile public task text into structured, retrieval-oriented intent."""
    original = _normalise(task)
    lowered = original.casefold()
    context_values = [
        _normalise(str(value)).casefold()
        for value in (*languages, *frameworks, *project_tags)
        if _normalise(str(value))
    ]

    technology = list(value for value in _TECHNOLOGIES if _contains(lowered, value))
    for value in context_values:
        if value not in technology:
            technology.append(value)
    technology_tuple = tuple(technology[:6])
    operation = _matched_values(lowered, _OPERATION_RULES)
    artifact = _matched_values(lowered, _ARTIFACT_RULES)
    constraints = _constraint_phrases(original)
    failure_mode = _failure_phrases(original)

    structured_parts = [
        *technology_tuple,
        *operation,
        *artifact,
        *constraints,
        *failure_mode,
        *_fallback_keywords(original),
    ]
    compressed = _bounded_query(structured_parts)
    if not compressed:
        compressed = original[:MAX_QUERY_CHARS]

    variants = tuple(dict.fromkeys(value for value in (original, compressed) if value))
    return CompiledIntent(
        original_query=original,
        compressed_query=compressed,
        technology=technology_tuple,
        operation=operation,
        artifact=artifact,
        constraints=constraints,
        failure_mode=failure_mode,
        query_variants=variants,
    )
