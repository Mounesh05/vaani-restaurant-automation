"""
rag_store.py  —  Advanced RAG pipeline for VAANI
─────────────────────────────────────────────────
ROOT CAUSE FIX:
  nomic-embed-text has a hard 512-token context limit (~300-350 words).
  The previous version sent chunks up to 600 characters which easily
  exceeded 512 tokens for dense menu text → Ollama 400 error.

    Fix:
        MAX_CHUNK_CHARS = 320  (safe ceiling ~200-250 tokens, well under 512)
        HARD_MAX_CHARS  = 420  (absolute truncation guard before Ollama call)
        BATCH_SIZE      = 10   (smaller batches, less memory pressure)

PARSING    : pdfplumber for PDF, plain read for .txt
CHUNKING   : Section-aware → paragraph → sentence-overlap splitting
EMBEDDING  : nomic-embed-text via Ollama (768-dim, cosine similarity)
STORAGE    : ChromaDB PersistentClient (disk-backed, local ./chroma_db/)
SPEED      : Module-level singleton client — no reconnect per query
             File hash cache — unchanged files skipped on re-index
"""

import os
import re
import hashlib
import json
import logging
import chromadb
from chromadb.utils import embedding_functions

logger = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
KNOWLEDGE_DIR    = "knowledge"
CHROMA_DIR       = "chroma_db"
COLLECTION       = "restaurant_knowledge"
EMBED_MODEL      = "nomic-embed-text"
OLLAMA_EMBED     = "http://localhost:11434/api/embeddings"

DEFAULT_MAX_DISTANCE = float(os.getenv("RAG_MAX_DISTANCE", "0.55"))
MIN_LEXICAL_OVERLAP = float(os.getenv("RAG_MIN_LEXICAL_OVERLAP", "0.08"))

MAX_CHUNK_CHARS  = 320   # focused chunks reduce retrieval drift and embedding truncation
HARD_MAX_CHARS   = 420   # absolute cap before embedding request
OVERLAP_SENTENCES = 1    # sentences carried over between adjacent chunks for context
BATCH_SIZE        = 10   # small but practical batch size for local Ollama stability

_LEXICAL_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "have", "has",
    "are", "was", "were", "our", "your", "you", "can", "will", "about",
    "into", "under", "over", "their", "they", "them", "not", "only", "but",
    "what", "when", "where", "which", "would", "could", "should", "please",
    "tell", "share", "give", "show", "restaurant", "restaurants",
}

# ── SINGLETON ─────────────────────────────────────────────────────────────────
_collection = None

def _get_collection():
    global _collection
    if _collection is not None:
        return _collection
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    emb_fn = embedding_functions.OllamaEmbeddingFunction(
        url=OLLAMA_EMBED,
        model_name=EMBED_MODEL
    )
    _collection = client.get_or_create_collection(
        COLLECTION,
        embedding_function=emb_fn,
        metadata={"hnsw:space": "cosine"}
    )
    return _collection


# ── PARSING ───────────────────────────────────────────────────────────────────
def _parse_pdf(path: str) -> str:
    try:
        import pdfplumber  # type: ignore[import-not-found]
        pages = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                table_lines = []
                for table in page.extract_tables():
                    for row in table:
                        cells = [str(c).strip() if c else "" for c in row]
                        filled = [c for c in cells if c]
                        if filled:
                            table_lines.append("  |  ".join(filled))
                raw = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                combined = raw + ("\n" + "\n".join(table_lines) if table_lines else "")
                pages.append(combined)
        return "\n".join(pages)
    except ImportError:
        from pypdf import PdfReader
        reader = PdfReader(path)
        return "\n".join(p.extract_text() or "" for p in reader.pages)


def _parse_txt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ── CHUNKING ──────────────────────────────────────────────────────────────────
SECTION_RE = re.compile(
    r"(SECTION\s+\d+\w*\s*[—\-\u2013]+\s*[A-Z][^\n]{3,})", re.IGNORECASE
)

def _sentences(text: str) -> list:
    parts = re.split(r'(?<=[.!?])\s+|\n', text.strip())
    return [p.strip() for p in parts if p.strip()]

def _split_paragraph(para: str) -> list:
    """
    Split one paragraph into chunks within MAX_CHUNK_CHARS.
    Chunks share OVERLAP_SENTENCES sentences for context continuity.
    All output chunks are hard-capped at HARD_MAX_CHARS before returning.
    """
    # Short enough already — just truncate and return
    if len(para) <= MAX_CHUNK_CHARS:
        return [para[:HARD_MAX_CHARS]]

    sents = _sentences(para)
    chunks, buf, buf_len = [], [], 0

    for sent in sents:
        if buf_len + len(sent) > MAX_CHUNK_CHARS and buf:
            chunk_text = " ".join(buf)
            chunks.append(chunk_text[:HARD_MAX_CHARS])
            buf = buf[-OVERLAP_SENTENCES:] if OVERLAP_SENTENCES else []
            buf_len = sum(len(s) for s in buf)
        buf.append(sent)
        buf_len += len(sent)

    if buf:
        chunks.append((" ".join(buf))[:HARD_MAX_CHARS])

    return chunks


def _chunk_document(text: str, source_label: str) -> list:
    """
    1. Split on SECTION headers (named domains)
    2. Split each section on blank lines (paragraphs)
    3. Split long paragraphs with sentence overlap
    4. Drop fragments under 40 chars
    """
    parts = SECTION_RE.split(text)
    chunks = []
    current_section = "General"
    i = 0

    while i < len(parts):
        part = parts[i].strip()
        if not part:
            i += 1
            continue
        if SECTION_RE.fullmatch(part):
            raw_section = part.strip().upper()
            if "MENU" in raw_section:
                current_section = "MENU"
            elif "RESTAURANT INFORMATION" in raw_section:
                current_section = "RESTAURANT INFORMATION"
            elif "FAQ" in raw_section or "FREQUENTLY ASKED" in raw_section:
                current_section = "FAQ"
            elif "POLICIES" in raw_section:
                current_section = "POLICIES"
            else:
                current_section = part.strip()
            i += 1
            continue

        paragraphs = [p.strip() for p in re.split(r'\n{2,}', part) if p.strip()]
        for para in paragraphs:
            if len(para) < 40:
                continue
            for sub in _split_paragraph(para):
                if sub.strip():
                    chunks.append({
                        "text":    sub.strip(),
                        "section": current_section,
                        "source":  source_label,
                    })
        i += 1

    return chunks


# ── FILE HASH CACHE ───────────────────────────────────────────────────────────
def _file_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()

def _load_hashes() -> dict:
    p = os.path.join(CHROMA_DIR, "_hashes.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}

def _save_hashes(hashes: dict):
    os.makedirs(CHROMA_DIR, exist_ok=True)
    with open(os.path.join(CHROMA_DIR, "_hashes.json"), "w") as f:
        json.dump(hashes, f)


# ── PUBLIC: BUILD INDEX ───────────────────────────────────────────────────────
def build_index(force: bool = False):
    col        = _get_collection()
    old_hashes = _load_hashes()
    new_hashes = {}
    total      = 0
    skipped    = 0

    files = [f for f in os.listdir(KNOWLEDGE_DIR)
             if f.lower().endswith((".pdf", ".txt"))]

    if not files:
        print(f"No .pdf or .txt files found in {KNOWLEDGE_DIR}/")
        return

    for fname in sorted(files):
        fpath = os.path.join(KNOWLEDGE_DIR, fname)
        fhash = _file_hash(fpath)
        new_hashes[fname] = fhash

        if not force and old_hashes.get(fname) == fhash:
            print(f"  Skipping {fname} (unchanged)")
            skipped += 1
            continue

        print(f"  Indexing {fname} ...")
        raw    = _parse_pdf(fpath) if fname.lower().endswith(".pdf") else _parse_txt(fpath)
        chunks = _chunk_document(raw, source_label=fname)

        if not chunks:
            print(f"  WARNING: no chunks from {fname}")
            continue

        # Remove stale documents for this source
        try:
            existing = col.get(where={"source": fname})
            if existing["ids"]:
                col.delete(ids=existing["ids"])
        except Exception:
            pass

        # Upsert in small batches — each batch document is hard-capped
        for start in range(0, len(chunks), BATCH_SIZE):
            batch     = chunks[start: start + BATCH_SIZE]
            safe_docs = [c["text"][:HARD_MAX_CHARS] for c in batch]   # FINAL GUARD

            col.upsert(
                documents=safe_docs,
                ids=[f"{fname}_{start + j}" for j in range(len(batch))],
                metadatas=[{"source": c["source"], "section": c["section"]}
                           for c in batch],
            )

        total += len(chunks)
        print(f"    -> {len(chunks)} chunks indexed")

    _save_hashes(new_hashes)
    print(f"\nDone.  {total} chunks indexed  |  {skipped} file(s) skipped (unchanged)")


# ── PUBLIC: RETRIEVE ──────────────────────────────────────────────────────────
def _lexical_tokens(text: str) -> set:
    tokens = re.findall(r"[a-zA-Z]{3,}", (text or "").lower())
    return {t for t in tokens if t not in _LEXICAL_STOPWORDS}


def _lexical_overlap(query: str, doc: str) -> float:
    q = _lexical_tokens(query)
    if not q:
        return 0.0
    d = _lexical_tokens(doc)
    if not d:
        return 0.0
    return len(q & d) / len(q)


def retrieve_hits(
    query: str,
    n: int = 4,
    section_filter: str = None,
    max_distance: float = DEFAULT_MAX_DISTANCE,
):
    """
    Returns top-n chunks as structured records:
      [{"text", "distance", "source", "section"}, ...]

    Any chunk whose cosine distance is above max_distance is dropped.
    Lower distance means stronger match.
    """
    try:
        col = _get_collection()
        where = {"section": section_filter} if section_filter else None
        query = (query or "").strip()

        # Fetch a wider candidate pool so lexical filtering still leaves enough hits.
        candidate_n = max(n * 3, 12)

        results = col.query(
            query_texts=[query],
            n_results=candidate_n,
            where=where,
            include=["documents", "distances", "metadatas"],
        )

        docs = results.get("documents", [[]])[0]
        distances = results.get("distances", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]

        hits = []
        seen = set()
        query_terms = _lexical_tokens(query)
        enforce_lexical_gate = section_filter is None
        for doc, dist, meta in zip(docs, distances, metadatas):
            if dist is None or dist > max_distance or not doc:
                continue

            lexical = _lexical_overlap(query, doc)
            if enforce_lexical_gate and len(query_terms) >= 2 and lexical < MIN_LEXICAL_OVERLAP:
                continue

            # Avoid duplicate chunks returning from neighbor vectors.
            key = doc.strip()
            if key in seen:
                continue
            seen.add(key)

            hits.append({
                "text": key,
                "distance": float(dist),
                "lexical_overlap": lexical,
                "source": (meta or {}).get("source"),
                "section": (meta or {}).get("section"),
            })

        hits.sort(key=lambda h: (-h.get("lexical_overlap", 0.0), h.get("distance", 1.0)))
        return hits[:n]

    except Exception as e:
        logger.error("RAG retrieve_hits error: %s", e)
        return []


def retrieve(
    query: str,
    n: int = 4,
    section_filter: str = None,
    max_distance: float = DEFAULT_MAX_DISTANCE,
) -> str:
    """
    Returns top-n most similar chunks as a single joined string.
    section_filter narrows the search to a specific section (e.g. 'MENU').
    Chunks with cosine distance > 0.65 are dropped as too weak.
    """
    hits = retrieve_hits(
        query=query,
        n=n,
        section_filter=section_filter,
        max_distance=max_distance,
    )
    return "\n\n".join(h["text"] for h in hits) if hits else ""


if __name__ == "__main__":
    build_index(force=True)