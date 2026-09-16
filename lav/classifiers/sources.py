"""Where each piece of an interaction comes from (LAV-92).

The classifier used to read the head of the session: user text, the first line of
each assistant message, tool names. Two sources were wrong for entity extraction:

- **Loaded skill bodies** are instructions, not conversation. They sit at the start
  of the session, so they filled the input window, and any names they contain
  (contact lists, examples) came back as `people`/`clients`.
- **Communication tool results** (mail, calendar, chat) name the real contacts of a
  session, and were never read at all: tool results are dropped in default mode.

This module segments a session by source and builds the communication block. The
tools that count as "communication" are deployment-specific and come from the
private taxonomy (`entities.communication_tools`, fnmatch patterns). Only the WHO
fields of their results are kept: senders, organizers, chat names, members,
subjects; email addresses are reduced to their domain; message bodies, previews,
phone numbers and ids never leave the machine.

Also used by `lav.entities` to decide how an entity was involved in a session.
"""

import fnmatch
import json
import re
from typing import Dict, Iterator, List, Optional, Tuple

from lav import taxonomy
from lav.classifiers.openai_classifier import _clean, _coerce_blocks

# Claude Code prepends this to the user message that carries a loaded SKILL.md.
SKILL_PREFIX = "Base directory for this skill:"
_SKILL_NAME_RE = re.compile(r"/skills/([^/\s]+)")

# Keys whose values say WHO is involved. Anything else in a result is dropped.
WHO_KEYS = {
    "subject", "from", "to", "cc", "sender", "organizer", "attendees", "topic", "name",
    "last_sender", "sender_name", "sender_display", "chat_name", "members", "displayName",
    "participants", "is_group",
}
# Transcript files read with the Read tool: only speaker names are kept.
TRANSCRIPT_SUFFIXES = (".vtt",)
_VTT_SPEAKER_RE = re.compile(r"<v ([^>]+)>")
_EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")

PER_RESULT_CHARS = 3000
COMM_BUDGET_CHARS = 12000

SEG_USER = "user"
SEG_ASSISTANT = "assistant"
SEG_SKILL = "skill"
SEG_COMM = "comm"
SEG_OTHER_TOOL = "other_tool"


def skill_marker(text: str) -> str:
    m = _SKILL_NAME_RE.search(text)
    return f"[skill loaded: {m.group(1) if m else '?'}]"


def is_communication_tool(name: str, tool_input: Optional[dict] = None,
                          patterns: Optional[List[str]] = None) -> bool:
    pats = taxonomy.COMMUNICATION_TOOL_PATTERNS if patterns is None else patterns
    if name == "Read" and str((tool_input or {}).get("file_path", "")).lower().endswith(TRANSCRIPT_SUFFIXES):
        return True
    return any(fnmatch.fnmatchcase(name or "", p) for p in pats)


def is_user_alias(name: str, aliases: Optional[List[str]] = None) -> bool:
    """True when every word of `name` is a word of one of the user's aliases.

    Word-based, so "Doe, Jane" and "Jane Doe" both match aliases ["Jane", "Jane Doe"],
    while "Janet" does not.
    """
    al = taxonomy.USER_ALIASES if aliases is None else aliases
    if not al:
        return False
    vocab = {t for a in al for t in re.findall(r"\w+", a.lower())}
    toks = re.findall(r"\w+", (name or "").lower())
    return bool(toks) and all(t in vocab for t in toks)


def _result_text(block: dict) -> str:
    tr = block.get("content", "")
    if isinstance(tr, list):
        tr = " ".join(x.get("text", "") for x in tr if isinstance(x, dict))
    return str(tr)


def _collect_names(obj, names: list):
    if isinstance(obj, list):
        for x in obj:
            _collect_names(x, names)
    elif isinstance(obj, dict):
        for k in ("displayName", "name", "emailAddress", "email", "address"):
            if isinstance(obj.get(k), str) and obj[k]:
                names.append(obj[k])
                return
        for v in obj.values():
            if isinstance(v, (list, dict)):
                _collect_names(v, names)
    elif isinstance(obj, str) and obj:
        names.append(obj)


def _who(obj, out: list):
    if isinstance(obj, list):
        for x in obj:
            _who(x, out)
    elif isinstance(obj, dict):
        pairs = []
        for k, v in obj.items():
            if k in WHO_KEYS:
                if isinstance(v, (list, dict)):
                    names: list = []
                    _collect_names(v, names)
                    if names:
                        pairs.append(f"{k}=" + ", ".join(dict.fromkeys(names)))
                elif v not in (None, "", 0, False):
                    pairs.append(f"{k}={v}")
            elif isinstance(v, (list, dict)):
                _who(v, out)
        if pairs:
            out.append("; ".join(pairs))


def who_lines(tool: str, raw: str, tool_input: Optional[dict] = None) -> List[str]:
    """Reduce one communication tool result to its WHO fields."""
    if tool == "Read" or tool.endswith("transcript"):
        speakers = _VTT_SPEAKER_RE.findall(raw)
        return ["speakers=" + ", ".join(dict.fromkeys(speakers))] if speakers else []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out: list = []
    _who(parsed, out)
    return [_EMAIL_RE.sub(r"@\1", line) for line in dict.fromkeys(out)]


def _tool_index(messages: List[Dict]) -> Dict[str, Tuple[str, dict]]:
    tools = {}
    for msg in messages:
        for b in _coerce_blocks(msg.get("content", "")) or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tools[b.get("id")] = (b.get("name", ""), b.get("input") or {})
    return tools


def segments(messages: List[Dict]) -> Iterator[Tuple[str, Optional[str], str]]:
    """Yield (segment, tool_name, text) over the whole session, uncapped.

    segment: user | assistant | skill | comm | other_tool. Tool results carry the
    RAW result text; callers decide what to keep.
    """
    tools = _tool_index(messages)
    for msg in messages:
        mtype = msg.get("type", "")
        blocks = _coerce_blocks(msg.get("content", ""))
        if blocks is None:
            text = _clean(str(msg.get("content", ""))).strip()
            if text:
                yield (SEG_SKILL if text.startswith(SKILL_PREFIX) else mtype, None, text)
            continue
        for b in blocks:
            if isinstance(b, str):
                if b.strip():
                    yield (mtype, None, _clean(b).strip())
                continue
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                text = _clean(b.get("text", "")).strip()
                if text:
                    yield (SEG_SKILL if text.startswith(SKILL_PREFIX) else mtype, None, text)
            elif bt == "tool_use":
                yield (mtype, b.get("name", ""), f"[tool: {b.get('name', '')}]")
            elif bt == "tool_result":
                name, inp = tools.get(b.get("tool_use_id"), ("", {}))
                seg = SEG_COMM if is_communication_tool(name, inp) else SEG_OTHER_TOOL
                yield (seg, name, _result_text(b))


def communication_block(messages: List[Dict]) -> str:
    """The COMMUNICATION DATA block appended to the classifier input ('' if none)."""
    tools = _tool_index(messages)
    chunks, used = [], 0
    for msg in messages:
        for b in _coerce_blocks(msg.get("content", "")) or []:
            if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                continue
            name, inp = tools.get(b.get("tool_use_id"), ("", {}))
            if not is_communication_tool(name, inp):
                continue
            chunk = "\n".join(who_lines(name, _result_text(b), inp))[:PER_RESULT_CHARS]
            if not chunk:
                continue
            # Bug fix: budget against the bytes actually appended below (the
            # "[toolname]\n" header + the "\n\n" separator joining entries), not just
            # the raw chunk. The old accounting undercounted both, so COMM_BUDGET_CHARS
            # was not really enforced: with many short results the real output could
            # grow well past the intended cap.
            entry = f"[{name.replace('mcp__', '')}]\n{chunk}"
            grow = len(entry) + (2 if chunks else 0)  # "\n\n" precedes all but the first
            if used + grow > COMM_BUDGET_CHARS:
                continue
            chunks.append(entry)
            used += grow
    if not chunks:
        return ""
    return ("\n\nCOMMUNICATION DATA (who/what fields from email, calendar and chat tools "
            "used in this session):\n" + "\n\n".join(chunks))
