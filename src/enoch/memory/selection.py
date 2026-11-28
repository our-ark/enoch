"""Cheap, model-free selection of long-term memories for conversation prompts.

A conversation session does not need every remembered fact on every request.
User preferences and authorization or workflow-policy memories are always
included, because dropping one could change what Enoch is allowed to do. The
remaining memories are chosen by keyword overlap with the current message and
then by recency, under a character budget. Superseded memories are never sent.

Nothing here calls a model or touches disk; callers pass loaded memories.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any


PINNED_TYPES = frozenset({"user_preference", "workflow_rule"})
PINNED_TAGS = frozenset(
    {
        "authorization",
        "identity",
        "merge",
        "permission",
        "pinned",
        "policy",
        "preference",
        "relationship",
        "workflow",
    }
)
# Authorization and policy vocabulary. Generous on purpose: a policy memory
# sent needlessly costs a few hundred characters, a policy memory dropped can
# change what Enoch does.
_POLICY_PATTERN = re.compile(
    "|".join(
        [
            r"authori[sz]",
            r"approv",
            r"permission",
            r"custodian",
            r"consent",
            r"\bmerge (?:request|authori[sz]ation|polic)",
            r"auto-?merge",
            r"--merge",
            r"\bnever merge",
            r"merge when ready",
            r"\bdo(?:n't| not) merge",
            r"\bpolic(?:y|ies)\b",
            r"\brules?\b",
            r"\bprefer",
            r"\bwants?\b",
            r"\baddress (?:him|her|them|me) as\b",
            r"\banswer\b[^.]{0,60}\bfirst\b",
            r"\bprivate (?:instance )?state\b",
            r"\b(?:disable|enable)[sd]?\b[^.]{0,40}\bautomatic",
            "授权",
            "批准",
            "决定权",
            "由人决定",
            "权限",
            "偏好",
            "希望",
            "规则",
            "政策",
            "不得公开",
        ]
    ),
    re.IGNORECASE,
)
_MEMORY_ID = re.compile(r"\bmem_\d{8}_\d{3}\b")
_LATIN = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿]+")
_SHORT_TERMS = frozenset({"ci", "pr", "qa", "ui", "ux"})
_STOPWORDS = frozenset(
    """
    about above after again all also and any anyone anything are because been
    before being but can could did does doing done everything for from get
    got had has have having her here hers him his how into its just know let
    like look maybe more most much need nothing not now off okay once only
    other our ours out over own please really right same see she should some
    someone something still such tell than thank thanks that the their theirs
    them then there these they think this those through today too under until
    very was way were what when where which while who whom why will with would
    yes yet you your yours
    """.split()
)
_SUFFIXES = ("ing", "ed", "es", "s", "e")


@dataclass(frozen=True)
class MemorySelection:
    """Memories chosen for one prompt, in display order."""

    memories: tuple[Mapping[str, Any], ...] = ()
    available: int = 0

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(memory_id(memory) for memory in self.memories)

    @property
    def omitted(self) -> int:
        return max(0, self.available - len(self.memories))

    def render(self) -> str:
        return "\n".join(memory_line(memory) for memory in self.memories)


def memory_id(memory: Mapping[str, Any]) -> str:
    return str(memory.get("id") or "").strip()


def memory_line(memory: Mapping[str, Any]) -> str:
    return f"- {memory.get('id')} [{memory.get('type')}] {memory.get('text')}"


def is_superseded(memory: Mapping[str, Any]) -> bool:
    return bool(memory.get("superseded_at") or memory.get("superseded_by"))


def active_memories(memories: Iterable[Any]) -> list[Mapping[str, Any]]:
    return [
        memory
        for memory in memories
        if isinstance(memory, Mapping)
        and str(memory.get("text") or "").strip()
        and not is_superseded(memory)
    ]


def is_pinned(memory: Mapping[str, Any]) -> bool:
    """User preferences and authorization or workflow-policy memories."""

    if str(memory.get("type") or "") in PINNED_TYPES:
        return True
    tags = memory.get("tags")
    if isinstance(tags, list) and PINNED_TAGS & {str(tag).strip().lower() for tag in tags}:
        return True
    return bool(_POLICY_PATTERN.search(str(memory.get("text") or "")))


def select_session_memories(
    memories: Iterable[Any],
    query: str,
    *,
    pinned_max_chars: int,
    relevant_max_chars: int,
    recent_count: int,
) -> MemorySelection:
    """Pinned memories, then memories relevant to ``query``, then recent ones."""

    active = active_memories(memories)
    pinned = [memory for memory in active if is_pinned(memory)]
    rest = [memory for memory in active if not is_pinned(memory)]
    chosen = _fill(_pinned_priority(pinned), pinned_max_chars)
    relevant = _ranked_relevant(rest, query, active)
    recent = sorted(rest, key=_recency_key, reverse=True)[: max(0, recent_count)]
    seen = {memory_id(memory) for memory in relevant}
    candidates = relevant + [memory for memory in recent if memory_id(memory) not in seen]
    chosen += _fill(candidates, relevant_max_chars)
    return MemorySelection(tuple(sorted(chosen, key=_display_key)), len(active))


def select_turn_memories(
    memories: Iterable[Any],
    query: str,
    *,
    exclude_ids: Collection[str],
    max_chars: int,
) -> MemorySelection:
    """Memories relevant to ``query`` that this session has not been shown yet."""

    active = active_memories(memories)
    pending = [memory for memory in active if memory_id(memory) not in exclude_ids]
    chosen = _fill(_ranked_relevant(pending, query, active), max_chars)
    return MemorySelection(tuple(sorted(chosen, key=_display_key)), len(pending))


def query_terms(text: str) -> frozenset[str]:
    lowered = str(text or "").lower()
    terms: set[str] = set(_MEMORY_ID.findall(lowered))
    for word in _LATIN.findall(lowered):
        if word in _SHORT_TERMS:
            terms.add(word)
            continue
        if len(word) < 3 or word in _STOPWORDS or word.isdigit():
            continue
        terms.add(_stem(word))
    for run in _CJK_RUN.findall(lowered):
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return frozenset(terms)


def _memory_terms(memory: Mapping[str, Any]) -> frozenset[str]:
    tags = memory.get("tags") if isinstance(memory.get("tags"), list) else []
    text = " ".join([memory_id(memory), str(memory.get("text") or ""), *map(str, tags)])
    return query_terms(text)


def _ranked_relevant(
    candidates: Sequence[Mapping[str, Any]],
    query: str,
    corpus: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    wanted = query_terms(query)
    if not wanted or not candidates:
        return []
    corpus_terms = [_memory_terms(memory) for memory in corpus]
    size = max(1, len(corpus_terms))
    frequency: dict[str, int] = {}
    for terms in corpus_terms:
        for term in terms & wanted:
            frequency[term] = frequency.get(term, 0) + 1
    distinctive_limit = max(2, size // 4)
    common_limit = max(2, size // 2)
    scored = []
    for memory in candidates:
        shared = _memory_terms(memory) & wanted
        if any(_MEMORY_ID.fullmatch(term) for term in shared):
            scored.append(((100, len(shared)), memory))
            continue
        distinctive = sum(1 for term in shared if frequency.get(term, 0) <= distinctive_limit)
        useful = sum(1 for term in shared if frequency.get(term, 0) <= common_limit)
        if distinctive >= 1 or useful >= 2:
            scored.append(((distinctive, useful), memory))
    scored.sort(key=lambda item: (item[0], _recency_key(item[1])), reverse=True)
    return [memory for _, memory in scored]


def _pinned_priority(memories: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    # Under an overflowing budget keep preferences first, then the newest
    # policies, which are the likeliest to refine older ones.
    newest = sorted(memories, key=_recency_key, reverse=True)
    return sorted(newest, key=lambda memory: str(memory.get("type") or "") != "user_preference")


def _fill(memories: Iterable[Mapping[str, Any]], max_chars: int) -> list[Mapping[str, Any]]:
    chosen: list[Mapping[str, Any]] = []
    used = 0
    for memory in memories:
        size = len(memory_line(memory)) + 1
        if used + size > max_chars:
            continue
        chosen.append(memory)
        used += size
    return chosen


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _recency_key(memory: Mapping[str, Any]) -> tuple[str, str]:
    return (str(memory.get("updated_at") or memory.get("created_at") or ""), memory_id(memory))


def _display_key(memory: Mapping[str, Any]) -> tuple[str, str]:
    return (str(memory.get("created_at") or ""), memory_id(memory))
