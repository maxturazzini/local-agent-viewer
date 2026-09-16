"""Entity graph (LAV-93): people and organizations, dated edges, mentions.

`interaction_metadata.people/clients` are tags: one list of strings per interaction.
As tags they cannot say that "Janie" and "Jane Smith" are one person, that a
workflow with 90 subagents is one piece of work, that a daily digest listing 25
senders is not 25 working relationships, or what a person IS to the user. This
module turns the tags into a small graph kept in three tables:

  entities         person | organization | self (the user), one row per name
  entity_edges     entity -> entity, ALWAYS dated: valid_from / valid_to
                   (NULL from = start unknown, NULL to = still valid)
                     alias_of   variant -> canonical name
                     works_for  person -> organization
                     client | prospect | partner | supplier | own | role_unknown
                                organization (or person) -> self
  entity_mentions  entity -> interaction, dated by the interaction's first/last
                   message, with HOW the entity was involved and the root session

Roles are never asked to the model: a role is a fact about the relationship, not
about one session, and a model re-deriving it per session disagrees with itself.
Roles come from `lav entities import` (a PRIVATE seed built from the user's own
records: address book, client folders, invoices) or from `lav entities confirm`.
Everything a rule proposes is stored with confirmed = 0.

Involvement is decided deterministically from where the name occurs in the
session (see lav.classifiers.sources), strongest first:
  conversation  user/assistant text
  data          results of a communication tool (mail, calendar, chat) read while working
  listing       any name surfaced by an automated run (taxonomy
                entities.automated_prompt_prefixes), e.g. a daily digest
  instructions  only inside a loaded skill body
  tool_output   only inside another tool's result
  not_found     nowhere in the session (rewritten or invented by the model)

Relations are also derived WITHOUT the model, from email addresses already stored in
the messages (entity_sightings): a person whose name is in the address, or who is the
display name next to it (chat members, meeting attendees), is proposed as works_for
the organization owning the domain. The edge is dated by the first time the address
was seen and stays a proposal (confirmed = 0). See derive_relations().

Everything is local to the node's DB and derived from interaction_metadata and
messages, so it is built on the node that classifies. No sync, like every other
backfill. Every step is idempotent: re-running build/import never duplicates, never
undoes a confirmation, never brings back a rejected proposal.
"""

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from lav import taxonomy

KIND_PERSON = "person"
KIND_ORG = "organization"
KIND_SELF = "self"
KINDS = (KIND_PERSON, KIND_ORG, KIND_SELF)

REL_ALIAS = "alias_of"
REL_WORKS_FOR = "works_for"
ROLE_RELATIONS = ("client", "prospect", "partner", "supplier", "own", "role_unknown")
EDGE_RELATIONS = (REL_ALIAS, REL_WORKS_FOR) + ROLE_RELATIONS

INV_CONVERSATION = "conversation"
INV_DATA = "data"
INV_LISTING = "listing"
INV_INSTRUCTIONS = "instructions"
INV_TOOL_OUTPUT = "tool_output"
INV_NOT_FOUND = "not_found"
INVOLVEMENTS = (INV_CONVERSATION, INV_DATA, INV_LISTING, INV_INSTRUCTIONS, INV_TOOL_OUTPUT, INV_NOT_FOUND)
# What counts as "working with" by default. `listing` is kept only for entities
# whose role is known, see list_entities().
WORKING_INVOLVEMENTS = (INV_CONVERSATION, INV_DATA)

FIELD_KIND = {"people": KIND_PERSON, "clients": KIND_ORG}
SELF_NAME = "self"

ENTITY_SCHEMA = """
-- Entity graph (LAV-93). See lav/entities.py.
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(kind, name)
);
CREATE INDEX IF NOT EXISTS idx_entities_key ON entities(kind, name_key);

CREATE TABLE IF NOT EXISTS entity_edges (
    id INTEGER PRIMARY KEY,
    src_id INTEGER NOT NULL REFERENCES entities(id),
    relation TEXT NOT NULL,
    dst_id INTEGER NOT NULL REFERENCES entities(id),
    valid_from TEXT,
    valid_to TEXT,
    date_basis TEXT,
    source TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entedges_src ON entity_edges(src_id, relation);
CREATE INDEX IF NOT EXISTS idx_entedges_dst ON entity_edges(dst_id, relation);

CREATE TABLE IF NOT EXISTS entity_mentions (
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    root_session_id TEXT NOT NULL,
    field TEXT NOT NULL,
    raw_name TEXT NOT NULL,
    involvement TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    model_used TEXT,
    built_at TEXT NOT NULL,
    PRIMARY KEY (entity_id, session_id, project_id, field)
);
CREATE INDEX IF NOT EXISTS idx_entmentions_session ON entity_mentions(session_id, project_id);
CREATE INDEX IF NOT EXISTS idx_entmentions_root ON entity_mentions(root_session_id);

-- Email addresses seen in an interaction (messages and tool results). Stored raw, per
-- session, so people created LATER still match addresses seen before.
CREATE TABLE IF NOT EXISTS entity_sightings (
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    address TEXT NOT NULL,
    domain TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL DEFAULT '',
    seen_at TEXT,
    PRIMARY KEY (session_id, project_id, address, display_name)
);
CREATE INDEX IF NOT EXISTS idx_entsightings_domain ON entity_sightings(domain);

-- Mail domains an organization is known to use (seed or manual).
CREATE TABLE IF NOT EXISTS entity_domains (
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    domain TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (entity_id, domain)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(ENTITY_SCHEMA)
    conn.commit()


def name_key(name: str) -> str:
    """Spelling-insensitive key: "Acme 360" == "Acme360", "Jose" == "José"."""
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(c for c in n if not unicodedata.combining(c))
    return re.sub(r"[^0-9a-z]", "", n.lower())


def match_text(text: str) -> str:
    """Text normalised for name lookup, padded so matches are whole words.

    Accents and punctuation become spaces, so "José Smith" is found in "jose.smith@"
    and "Smith, José" is not mistaken for "Smithson".
    """
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " " + re.sub(r"[^0-9a-z]+", " ", t).strip() + " "


def root_session(session_id: str) -> str:
    """Subagent sessions are stored as "<root>::agent-<id>": count them once, as the root."""
    return (session_id or "").split("::", 1)[0]


# ── entities & edges ───────────────────────────────────────────────────────

def find_entity(conn, kind: str, name: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM entities WHERE kind=? AND name=?", (kind, name)).fetchone()
    if row:
        return row[0]
    key = name_key(name)
    if not key:
        return None
    row = conn.execute("SELECT id FROM entities WHERE kind=? AND name_key=? ORDER BY id LIMIT 1",
                       (kind, key)).fetchone()
    return row[0] if row else None


def get_or_create(conn, kind: str, name: str) -> Tuple[int, bool]:
    if kind not in KINDS:
        raise ValueError(f"unknown entity kind {kind!r}")
    name = (name or "").strip()
    if not name:
        raise ValueError("empty entity name")
    eid = find_entity(conn, kind, name)
    if eid:
        return eid, False
    cur = conn.execute("INSERT INTO entities(kind, name, name_key, created_at) VALUES (?,?,?,?)",
                       (kind, name, name_key(name), _now()))
    return cur.lastrowid, True


def self_id(conn, name: Optional[str] = None) -> int:
    row = conn.execute("SELECT id FROM entities WHERE kind=? ORDER BY id LIMIT 1", (KIND_SELF,)).fetchone()
    if row:
        return row[0]
    return get_or_create(conn, KIND_SELF, name or SELF_NAME)[0]


def add_edge(conn, src_id: int, relation: str, dst_id: int, valid_from: Optional[str] = None,
             valid_to: Optional[str] = None, date_basis: Optional[str] = None, source: str = "manual",
             confirmed: bool = False, note: Optional[str] = None) -> Tuple[int, bool]:
    """Insert or update the edge identified by (src, relation, dst, valid_from).

    A different valid_from is a different period of the same relationship, so it is a
    new row: that is how history is kept. A confirmation is never downgraded.
    """
    if relation not in EDGE_RELATIONS:
        raise ValueError(f"unknown relation {relation!r}")
    row = conn.execute(
        "SELECT id, confirmed FROM entity_edges WHERE src_id=? AND relation=? AND dst_id=? "
        "AND IFNULL(valid_from, '') = IFNULL(?, '')", (src_id, relation, dst_id, valid_from)).fetchone()
    now = _now()
    if row:
        if int(row[1]) == -1 and not confirmed:
            return row[0], False  # rejected by the user: a rule or a re-import does not bring it back
        conn.execute(
            "UPDATE entity_edges SET valid_to=?, date_basis=COALESCE(?, date_basis), source=?, "
            "confirmed=?, note=COALESCE(?, note), updated_at=? WHERE id=?",
            (valid_to, date_basis, source, max(int(row[1]), int(bool(confirmed))), note, now, row[0]))
        return row[0], False
    cur = conn.execute(
        "INSERT INTO entity_edges(src_id, relation, dst_id, valid_from, valid_to, date_basis, source, "
        "confirmed, note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (src_id, relation, dst_id, valid_from, valid_to, date_basis, source, int(bool(confirmed)), note, now, now))
    return cur.lastrowid, True


def canonical_id(conn, entity_id: int) -> int:
    """Follow CONFIRMED alias_of edges. Proposals change nothing until confirmed."""
    seen = set()
    cur = entity_id
    while cur not in seen:
        seen.add(cur)
        row = conn.execute(
            "SELECT dst_id FROM entity_edges WHERE src_id=? AND relation=? AND confirmed=1 "
            "AND valid_to IS NULL ORDER BY id LIMIT 1", (cur, REL_ALIAS)).fetchone()
        if not row:
            return cur
        cur = row[0]
    return cur


def _valid_at(alias: str, at: Optional[str]) -> Tuple[str, list]:
    if not at:
        return f"{alias}.valid_to IS NULL", []
    return (f"(IFNULL(substr({alias}.valid_from,1,10), '') <= ? AND "
            f"(substr({alias}.valid_to,1,10) >= ? OR {alias}.valid_to IS NULL))", [at, at])


def role_of(conn, entity_id: int, at: Optional[str] = None) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """(role, organization_id, organization_name) valid at `at` (default: now, i.e. open edges).

    A direct role edge wins; otherwise the role of the organization the person works for.
    Role edges count only when confirmed: a role is the user's statement. works_for may
    still be a proposal (a matching mail domain is strong evidence), confirmed ones first.
    role_unknown is returned as None.
    """
    me = self_id(conn)
    cond, params = _valid_at("e", at)
    row = conn.execute(
        f"SELECT relation FROM entity_edges e WHERE src_id=? AND dst_id=? AND relation IN "
        f"({','.join('?' * len(ROLE_RELATIONS))}) AND confirmed=1 AND {cond} ORDER BY id DESC LIMIT 1",
        [entity_id, me, *ROLE_RELATIONS, *params]).fetchone()
    if row and row[0] != "role_unknown":
        return row[0], None, None
    cond_w, params_w = _valid_at("w", at)
    cond_r, params_r = _valid_at("r", at)
    row = conn.execute(
        f"SELECT r.relation, o.id, o.name FROM entity_edges w "
        f"JOIN entities o ON o.id = w.dst_id "
        f"LEFT JOIN entity_edges r ON r.src_id = o.id AND r.dst_id = ? AND r.confirmed = 1 "
        f"  AND r.relation IN ({','.join('?' * len(ROLE_RELATIONS))}) AND {cond_r} "
        f"WHERE w.src_id = ? AND w.relation = ? AND {cond_w} "
        f"ORDER BY w.confirmed DESC, w.id DESC LIMIT 1",
        [me, *ROLE_RELATIONS, *params_r, entity_id, REL_WORKS_FOR, *params_w]).fetchone()
    if row:
        role = row[0] if row[0] and row[0] != "role_unknown" else None
        return role, row[1], row[2]
    return None, None, None


# ── mentions ───────────────────────────────────────────────────────────────

def involvement_of(name: str, texts: Dict[str, str], automated: bool) -> str:
    from lav.classifiers import sources

    n = match_text(name)
    if not n.strip():
        return INV_NOT_FOUND
    in_conv = n in texts.get("conversation", "")
    in_comm = n in texts.get(sources.SEG_COMM, "")
    if automated and (in_conv or in_comm):
        return INV_LISTING
    if in_conv:
        return INV_CONVERSATION
    if in_comm:
        return INV_DATA
    if n in texts.get(sources.SEG_SKILL, ""):
        return INV_INSTRUCTIONS
    if n in texts.get(sources.SEG_OTHER_TOOL, ""):
        return INV_TOOL_OUTPUT
    return INV_NOT_FOUND


def _session_texts(messages: List[Dict]) -> Tuple[Dict[str, str], str]:
    from lav.classifiers import sources

    parts: Dict[str, List[str]] = {}
    first_user = ""
    for seg, _tool, text in sources.segments(messages):
        key = "conversation" if seg in (sources.SEG_USER, sources.SEG_ASSISTANT) else seg
        parts.setdefault(key, []).append(match_text(text))
        if seg == sources.SEG_USER and not first_user:
            first_user = text
    return {k: "\n".join(v) for k, v in parts.items()}, first_user


def is_automated(first_user_text: str, prefixes: Optional[Iterable[str]] = None) -> bool:
    pre = taxonomy.AUTOMATED_PROMPT_PREFIXES if prefixes is None else list(prefixes)
    t = (first_user_text or "").lstrip()
    return any(t.startswith(p) for p in pre)


def build_mentions(conn, since: Optional[str] = None, until: Optional[str] = None,
                   project: Optional[str] = None, dry_run: bool = False) -> Dict:
    """Rebuild entity_mentions for the classified interactions in a time window.

    Idempotent: an interaction's mentions are deleted and re-derived from its current
    metadata, so re-running after a reclassification replaces them.
    """
    from lav.classifiers import sources

    ensure_schema(conn)
    sql = ("SELECT m.session_id, m.project_id, m.people, m.clients, m.model_used "
           "FROM interaction_metadata m JOIN interactions i "
           "ON i.session_id = m.session_id AND i.project_id = m.project_id ")
    where, params = [], []
    if project:
        sql += "JOIN projects p ON p.id = i.project_id "
        where.append("p.name = ?")
        params.append(project)
    if since:
        where.append("i.timestamp >= ?")
        params.append(since)
    if until:
        where.append("i.timestamp < ?")
        params.append(until)
    if where:
        sql += "WHERE " + " AND ".join(where)
    rows = conn.execute(sql + " ORDER BY i.timestamp", params).fetchall()

    stats = {"interactions": len(rows), "automated_interactions": 0, "mentions": 0,
             "by_involvement": {k: 0 for k in INVOLVEMENTS}, "entities_created": 0,
             "user_aliases_skipped": 0, "alias_proposals": 0, "dry_run": dry_run}
    for session_id, project_id, people, clients, model_used in rows:
        msgs = [{"type": t, "content": c, "timestamp": ts} for t, c, ts in conn.execute(
            "SELECT type, content, timestamp FROM messages WHERE session_id=? AND project_id=? ORDER BY id",
            (session_id, project_id))]
        stamps = [m["timestamp"] for m in msgs if m["timestamp"]]
        texts, first_user = _session_texts(msgs)
        automated = is_automated(first_user)
        stats["automated_interactions"] += int(automated)
        conn.execute("DELETE FROM entity_mentions WHERE session_id=? AND project_id=?", (session_id, project_id))
        conn.execute("DELETE FROM entity_sightings WHERE session_id=? AND project_id=?", (session_id, project_id))
        conn.executemany(
            "INSERT OR IGNORE INTO entity_sightings(session_id, project_id, address, domain, display_name, tool, seen_at) "
            "VALUES (?,?,?,?,?,?,?)", [(session_id, project_id, *sg) for sg in extract_sightings(msgs)])
        for field, raw in (("people", people), ("clients", clients)):
            try:
                names = json.loads(raw or "[]")
            except (ValueError, TypeError):
                names = []
            for name in dict.fromkeys(n.strip() for n in names if isinstance(n, str) and n.strip()):
                if field == "people" and sources.is_user_alias(name):
                    stats["user_aliases_skipped"] += 1
                    continue
                eid, created = get_or_create(conn, FIELD_KIND[field], name)
                stats["entities_created"] += int(created)
                inv = involvement_of(name, texts, automated)
                stats["by_involvement"][inv] += 1
                stats["mentions"] += 1
                conn.execute(
                    "INSERT OR REPLACE INTO entity_mentions(entity_id, session_id, project_id, root_session_id, "
                    "field, raw_name, involvement, valid_from, valid_to, model_used, built_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, session_id, project_id, root_session(session_id), field, name, inv,
                     min(stamps) if stamps else None, max(stamps) if stamps else None, model_used, _now()))
    # Relations first: they can create organizations, which the alias rules must see in the
    # same round, or the next round would propose new aliases on unchanged data.
    stats["relations"] = derive_relations(conn, commit=not dry_run)
    stats["alias_proposals"] = propose_aliases(conn)
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return stats


def propose_aliases(conn) -> int:
    """Propose alias_of edges for partial person names, confirmed = 0.

    "Maria" -> the ONLY person whose first or last name is "Maria"; "Mar Lop" -> the only
    person whose name words start with "ann" and "ros". Ambiguous names get nothing:
    a wrong merge is worse than a duplicate, and the user confirms either way.
    """
    people = conn.execute("SELECT id, name FROM entities WHERE kind=?", (KIND_PERSON,)).fetchall()
    full = [(i, n, [w.lower() for w in re.findall(r"\w+", n)]) for i, n in people]
    full = [f for f in full if len(f[2]) >= 2]
    has_alias = {r[0] for r in conn.execute("SELECT src_id FROM entity_edges WHERE relation=?", (REL_ALIAS,))}
    proposed = 0
    for eid, name in people:
        if eid in has_alias:
            continue
        words = [w.lower() for w in re.findall(r"\w+", name) if w.lower() not in ("dr", "dott", "ing", "prof")]
        if not words:
            continue
        if len(words) == 1:
            w = words[0]
            cands = [f for f in full if f[0] != eid and (w in (f[2][0], f[2][-1])
                                                        or (len(w) >= 3 and f[2][0].startswith(w)))]
        else:
            cands = [f for f in full if f[0] != eid and len(f[2]) == len(words) and f[2] != words
                     and all(fw.startswith(w) for fw, w in zip(f[2], words))]
        if len(cands) == 1:
            _, created = add_edge(conn, eid, REL_ALIAS, cands[0][0], source="rule:partial_name")
            proposed += int(created)
    return proposed + _propose_org_aliases(conn, has_alias)


# Legal forms and fillers that do not identify an organization.
_ORG_NOISE_WORDS = {"srl", "spa", "sas", "snc", "s", "r", "l", "p", "a", "ltd", "inc", "llc", "gmbh",
                    "group", "gruppo", "the", "di", "de", "del", "la", "le", "il"}


def _org_words(name: str) -> frozenset:
    return frozenset(w for w in match_text(name).split() if w not in _ORG_NOISE_WORDS)


def _propose_org_aliases(conn, has_alias: set) -> int:
    """Organizations: same words in another order ("Tools Acme" / "Acme Tools"),
    or a name whose words are all inside exactly ONE longer name ("Acme" -> "Acme
    Holding"). Proposals only, like people: "Acme" may well be a different Acme."""
    orgs = [(i, n, _org_words(n)) for i, n in conn.execute("SELECT id, name FROM entities WHERE kind=?", (KIND_ORG,))]
    me = self_id(conn)
    with_role = {r[0] for r in conn.execute(
        f"SELECT src_id FROM entity_edges WHERE dst_id=? AND confirmed=1 AND relation IN "
        f"({','.join('?' * len(ROLE_RELATIONS))})", (me, *ROLE_RELATIONS))}
    proposed = 0
    # An organization with a known role absorbs the longer names built on it ("AI-Team Acme",
    # "Acme Italia"): those are its projects and spellings, the role is the anchor.
    for eid, _name, words in orgs:
        if eid not in with_role or not words:
            continue
        for oid, _oname, owords in orgs:
            if oid != eid and oid not in has_alias and oid not in with_role and words < owords:
                _, created = add_edge(conn, oid, REL_ALIAS, eid, source="rule:built_on_known_org")
                proposed += int(created)
                has_alias.add(oid)
    # Passes run to completion one after the other, each seeing what the previous one
    # aliased: interleaving them makes the result depend on row order, and a second run
    # on unchanged data would then propose something new.
    same_words = set()
    for eid, _name, words in orgs:
        if eid in has_alias or not words:
            continue
        same = sorted(o[0] for o in orgs if o[0] != eid and o[2] == words)
        if same:
            same_words.add(eid)
            if eid > same[0]:  # one direction only: newer spelling -> oldest
                _, created = add_edge(conn, eid, REL_ALIAS, same[0], source="rule:same_words")
                proposed += int(created)
                has_alias.add(eid)
    for eid, _name, words in orgs:
        if eid in has_alias or eid in same_words or not words:
            continue
        wider = [o for o in orgs if o[0] != eid and words < o[2] and o[0] not in has_alias]
        if len(wider) == 1:
            _, created = add_edge(conn, eid, REL_ALIAS, wider[0][0], source="rule:name_inside")
            proposed += int(created)
    return proposed


# ── relations from email addresses (no model) ──────────────────────────────

_ADDRESS_RE = re.compile(r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]*)@((?:[a-z0-9-]+\.)+[a-z]{2,24})(?![\w-])", re.I)
# Addresses that say nothing about an employer.
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "outlook.it", "hotmail.com", "hotmail.it", "live.com",
    "live.it", "msn.com", "icloud.com", "me.com", "mac.com", "yahoo.com", "yahoo.it", "libero.it",
    "virgilio.it", "tiscali.it", "alice.it", "tim.it", "fastwebnet.it", "proton.me", "protonmail.com",
    "gmx.com", "gmx.net", "aol.com", "pec.it", "legalmail.it",
}
TECHNICAL_DOMAIN_SUFFIXES = (".onmicrosoft.com", "noreply.github.com", ".local", "example.com", "example.org")
_NO_REPLY_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|notifications?|mailer-daemon|postmaster|bounce)", re.I)
_NAME_KEYS = ("displayName", "display_name", "name", "sender_name", "sender_display")
_ADDR_KEYS = ("email", "emailAddress", "address", "mail", "userPrincipalName", "from", "sender")


def _is_useful_domain(domain: str, local: str = "") -> bool:
    d = domain.lower()
    return (d not in FREE_MAIL_DOMAINS and not d.endswith(TECHNICAL_DOMAIN_SUFFIXES)
            and not _NO_REPLY_LOCAL.match(local or ""))


def _named_addresses(obj, out: list):
    """(display_name, address) pairs that sit in the SAME record: chat members, attendees."""
    if isinstance(obj, list):
        for x in obj:
            _named_addresses(x, out)
    elif isinstance(obj, dict):
        name = next((obj[k] for k in _NAME_KEYS if isinstance(obj.get(k), str) and obj[k].strip()), None)
        addr = None
        for k in _ADDR_KEYS:
            v = obj.get(k)
            if isinstance(v, dict):  # {"emailAddress": {"name": ..., "address": ...}}
                name = name or v.get("name")
                v = v.get("address")
            if isinstance(v, str) and _ADDRESS_RE.fullmatch(v.strip()):
                addr = v.strip().lower()
                break
        if name and addr and "@" not in name:
            out.append((name.strip(), addr))
        for v in obj.values():
            if isinstance(v, (list, dict)):
                _named_addresses(v, out)


def extract_sightings(messages: List[Dict]) -> List[Tuple[str, str, str, str, Optional[str]]]:
    """(address, domain, display_name, tool, seen_at) for every address in the session."""
    from lav.classifiers import sources

    seen: Dict[Tuple[str, str], Tuple[str, str, str, str, Optional[str]]] = {}
    stamp_of = {}
    tools = sources._tool_index(messages)
    for msg in messages:
        blocks = sources._coerce_blocks(msg.get("content", ""))
        texts = []
        if blocks is None:
            texts.append(("", str(msg.get("content", ""))))
        else:
            for b in blocks:
                if isinstance(b, str):
                    texts.append(("", b))
                elif isinstance(b, dict) and b.get("type") == "text":
                    texts.append(("", b.get("text", "")))
                elif isinstance(b, dict) and b.get("type") == "tool_result":
                    texts.append((tools.get(b.get("tool_use_id"), ("", {}))[0], sources._result_text(b)))
        for tool, text in texts:
            if "@" not in text:
                continue
            try:
                parsed = json.loads(text) if tool else None
            except (ValueError, TypeError):
                parsed = None
            pairs: list = []
            if parsed is not None:
                _named_addresses(parsed, pairs)
            for name, addr in pairs:
                local, _, dom = addr.partition("@")
                if _is_useful_domain(dom, local):
                    seen[(addr, name)] = (addr, dom, name, tool, msg.get("timestamp"))
            for local, dom in _ADDRESS_RE.findall(text):
                addr = f"{local}@{dom}".lower()
                if _is_useful_domain(dom, local) and (addr, "") not in seen:
                    seen[(addr, "")] = (addr, dom.lower(), "", tool, msg.get("timestamp"))
            stamp_of.setdefault(tool, msg.get("timestamp"))
    return list(seen.values())


def _address_matches(name: str, local: str) -> bool:
    """"Jane Smith" matches jane.smith@, j.smith@, smith.jane@, jsmith@ (last name >= 4)."""
    words = [w for w in match_text(name).split() if len(w) >= 2]
    if len(words) < 2:
        return False
    lt = [t for t in re.split(r"[^a-z]+", local.lower()) if t]
    if not lt:
        return False
    flat = "".join(lt)
    for first, last in ((words[0], words[-1]), (words[-1], words[0])):
        if len(last) < 3:
            continue
        if last in lt and (first in lt or first[0] in lt):
            return True
        if len(last) >= 4 and flat in (first[0] + last, first + last, last + first):
            return True
    return False


def _domain_label_key(domain: str) -> str:
    return name_key(domain.split(".")[0])


def _org_for_domain(conn, domain: str, cache: Dict[str, Optional[int]]) -> Tuple[Optional[int], str]:
    if domain in cache:
        return cache[domain], "cached"
    row = conn.execute("SELECT entity_id FROM entity_domains WHERE domain=?", (domain,)).fetchone()
    if row:
        cache[domain] = canonical_id(conn, row[0])
        return cache[domain], "known_domain"
    label = _domain_label_key(domain)
    if len(label) < 3:
        cache[domain] = None
        return None, ""
    me = self_id(conn)
    with_role = {r[0] for r in conn.execute(
        f"SELECT src_id FROM entity_edges WHERE dst_id=? AND confirmed=1 AND relation IN "
        f"({','.join('?' * len(ROLE_RELATIONS))})", (me, *ROLE_RELATIONS))}
    cands = []
    domain_named = {r[0] for r in conn.execute("SELECT entity_id FROM entity_domains WHERE source='rule:unmatched_domain'")}
    for oid, key in conn.execute("SELECT id, name_key FROM entities WHERE kind=?", (KIND_ORG,)):
        # Organizations this rule created from a bare domain are found by their domain only:
        # matching them by name would make the result depend on what earlier runs created.
        if canonical_id(conn, oid) != oid or not key or oid in domain_named:
            continue
        if key == label or (len(label) >= 4 and len(key) >= 4 and (key in label or label in key)):
            cands.append(oid)
    if len(cands) > 1:
        ranked = [c for c in cands if c in with_role] or cands
        exact = [c for c in ranked if conn.execute("SELECT name_key FROM entities WHERE id=?", (c,)).fetchone()[0] == label]
        cands = exact or ranked
    cache[domain] = cands[0] if len(cands) == 1 else None
    return cache[domain], "name_in_domain" if len(cands) == 1 else ""


def derive_relations(conn, min_sessions: int = 1, commit: bool = True) -> Dict:
    """Propose works_for edges from the addresses in entity_sightings. Idempotent.

    A person matches an address when the address sits next to their display name in a
    record (strong) or carries their name in the local part. The organization is the one
    owning the domain (entity_domains) or the one whose name IS the domain label; with
    neither, a new organization named after the domain is created, so the evidence is
    visible instead of lost. One edge per person/organization, dated by the first
    sighting; the note says how often and until when it was seen.
    """
    persons = [(i, n) for i, n in conn.execute("SELECT id, name FROM entities WHERE kind=?", (KIND_PERSON,))
               if canonical_id(conn, i) == i and len(match_text(n).split()) >= 2]
    by_display = {}
    for i, n in persons:
        by_display.setdefault(match_text(n), i)
    from lav.classifiers import sources

    evidence: Dict[Tuple[int, str], Dict] = {}
    for addr, domain, display, session_id, seen_at in conn.execute(
            "SELECT address, domain, display_name, session_id, seen_at FROM entity_sightings"):
        local = addr.split("@", 1)[0]
        hits = []
        if display and by_display.get(match_text(display)):
            hits = [(by_display[match_text(display)], "display_name")]
        else:
            hits = [(i, "address") for i, n in persons if _address_matches(n, local)]
        for pid, how in hits:
            if sources.is_user_alias(conn.execute("SELECT name FROM entities WHERE id=?", (pid,)).fetchone()[0]):
                continue
            e = evidence.setdefault((pid, domain), {"sessions": set(), "first": None, "last": None, "how": set(), "addr": set()})
            e["sessions"].add(session_id)
            e["how"].add(how)
            e["addr"].add(addr)
            if seen_at:
                e["first"] = min(filter(None, [e["first"], seen_at]))
                e["last"] = max(filter(None, [e["last"], seen_at]))

    stats = {"candidates": len(evidence), "edges_created": 0, "edges_updated": 0, "skipped_known": 0,
             "skipped_rejected": 0, "orgs_created": 0, "unmatched_domains": 0}
    cache: Dict[str, Optional[int]] = {}
    for (pid, domain), e in sorted(evidence.items()):
        # A name next to the address is strong on its own; a name guessed from the address
        # alone needs to come back in a second session (lists of example addresses, typos).
        needed = min_sessions if "display_name" in e["how"] else max(min_sessions, 2)
        if len(e["sessions"]) < needed:
            stats["below_threshold"] = stats.get("below_threshold", 0) + 1
            continue
        org, _ = _org_for_domain(conn, domain, cache)
        if org is None:
            org, created = get_or_create(conn, KIND_ORG, domain)
            stats["orgs_created"] += int(created)
            conn.execute("INSERT OR IGNORE INTO entity_domains(entity_id, domain, source) VALUES (?,?,?)",
                         (org, domain, "rule:unmatched_domain"))
            cache[domain] = org
            stats["unmatched_domains"] += int(created)
        rows = conn.execute("SELECT id, confirmed, source, valid_from FROM entity_edges WHERE src_id=? AND relation=? "
                            "AND dst_id=?", (pid, REL_WORKS_FOR, org)).fetchall()
        if any(r[1] == -1 for r in rows):
            stats["skipped_rejected"] += 1
            continue
        if any(r[1] == 1 or not str(r[2]).startswith("rule:mail") for r in rows):
            stats["skipped_known"] += 1
            continue
        how = "display_name" if "display_name" in e["how"] else "address"
        note = (f"{len(e['sessions'])} sessioni, visto dal {str(e['first'])[:10]} al {str(e['last'])[:10]}, "
                f"{'nome accanto all indirizzo' if how == 'display_name' else 'nome nell indirizzo'}: "
                f"{', '.join(sorted(e['addr'])[:3])}")
        first = str(e["first"])[:10] if e["first"] else None
        if rows:
            vf = min(filter(None, [rows[0][3], first]), default=None)
            cur = conn.execute("SELECT valid_from, note, source FROM entity_edges WHERE id=?", (rows[0][0],)).fetchone()
            if cur != (vf, note, f"rule:mail_{how}"):
                conn.execute("UPDATE entity_edges SET valid_from=?, date_basis='mail_seen', note=?, source=?, "
                             "updated_at=? WHERE id=?", (vf, note, f"rule:mail_{how}", _now(), rows[0][0]))
                stats["edges_updated"] += 1
        else:
            add_edge(conn, pid, REL_WORKS_FOR, org, valid_from=first, date_basis="mail_seen",
                     source=f"rule:mail_{how}", confirmed=False, note=note)
            stats["edges_created"] += 1
    if commit:
        conn.commit()
    return stats


# ── seed import ────────────────────────────────────────────────────────────

def _ref(conn, ref) -> int:
    if ref == KIND_SELF or ref == [KIND_SELF] or (isinstance(ref, (list, tuple)) and ref and ref[0] == KIND_SELF):
        return self_id(conn)
    if isinstance(ref, dict):
        ref = (ref.get("kind"), ref.get("name"))
    kind, name = ref
    return get_or_create(conn, kind, name)[0]


def import_seed(conn, seed: Dict, source_label: str = "seed") -> Dict:
    """Load a seed: {"self": name, "entities": [...], "edges": [...]}. See docs/entities.seed.example.json."""
    ensure_schema(conn)
    stats = {"entities": 0, "aliases": 0, "edges_created": 0, "edges_updated": 0}
    if seed.get("self"):
        me = self_id(conn, seed["self"])
        conn.execute("UPDATE entities SET name=?, name_key=? WHERE id=?", (seed["self"], name_key(seed["self"]), me))
    for ent in seed.get("entities", []):
        eid, created = get_or_create(conn, ent["kind"], ent["name"])
        stats["entities"] += int(created)
        # Same name_key, different spelling ("ACMECORP" written by the model, "Acme Corp" in the
        # records): the user's records set the display name.
        current = conn.execute("SELECT name FROM entities WHERE id=?", (eid,)).fetchone()[0]
        if current != ent["name"] and not conn.execute(
                "SELECT 1 FROM entities WHERE kind=? AND name=?", (ent["kind"], ent["name"])).fetchone():
            conn.execute("UPDATE entities SET name=? WHERE id=?", (ent["name"], eid))
            stats["renamed"] = stats.get("renamed", 0) + 1
        for domain in ent.get("domains", []):
            conn.execute("INSERT OR REPLACE INTO entity_domains(entity_id, domain, source) VALUES (?,?,?)",
                         (eid, domain.lower().strip(), f"seed:{source_label}"))
        for alias in ent.get("aliases", []):
            aid, _ = get_or_create(conn, ent["kind"], alias)
            if aid != eid:
                _, c = add_edge(conn, aid, REL_ALIAS, eid, source=f"seed:{source_label}", confirmed=True)
                stats["aliases"] += int(c)
    me = self_id(conn)
    for e in seed.get("edges", []):
        src, dst = _ref(conn, e["src"]), _ref(conn, e["dst"])
        if e["relation"] in ROLE_RELATIONS and dst == me and not e.get("valid_to"):
            # prospect -> client: the old period ends where the new one starts. Only edges the
            # seed itself created are closed; a role set by hand is the user's to close.
            end = e.get("valid_from") or _now()[:10]
            closed = conn.execute(
                f"UPDATE entity_edges SET valid_to=?, updated_at=? WHERE src_id=? AND dst_id=? AND valid_to IS NULL "
                f"AND relation IN ({','.join('?' * len(ROLE_RELATIONS))}) AND relation != ? AND source LIKE 'seed:%'",
                (end, _now(), src, dst, *ROLE_RELATIONS, e["relation"])).rowcount
            stats["roles_closed"] = stats.get("roles_closed", 0) + closed
        _, created = add_edge(conn, src, e["relation"], dst,
                              valid_from=e.get("valid_from"), valid_to=e.get("valid_to"),
                              date_basis=e.get("date_basis"), source=f"seed:{source_label}",
                              confirmed=bool(e.get("confirmed", False)), note=e.get("note"))
        stats["edges_created" if created else "edges_updated"] += 1
    # New names from the records can resolve partial names seen before, or make them ambiguous;
    # new domains can resolve addresses seen before.
    stats["relations"] = derive_relations(conn, commit=False)
    stats["alias_proposals"] = propose_aliases(conn)
    conn.commit()
    return stats


# ── reading ────────────────────────────────────────────────────────────────

def list_entities(conn, kind: Optional[str] = None, at: Optional[str] = None, since: Optional[str] = None,
                  until: Optional[str] = None, all_involvements: bool = False,
                  role: Optional[str] = None) -> List[Dict]:
    """Canonical entities with their role at `at` and how often they were involved.

    Counting unit = ROOT session (a workflow's subagents count once). By default only
    working involvements count (conversation, data); `listing` is counted too when the
    entity has a known role, so a digest keeps clients and team and drops strangers.
    """
    ensure_schema(conn)
    where, params = ["1=1"], []
    if kind:
        where.append("e.kind = ?")
        params.append(kind)
    if since:
        where.append("substr(m.valid_from,1,10) >= ?")
        params.append(since)
    if until:
        where.append("substr(m.valid_from,1,10) < ?")
        params.append(until)
    rows = conn.execute(
        f"SELECT m.entity_id, m.root_session_id, m.involvement, m.valid_from FROM entity_mentions m "
        f"JOIN entities e ON e.id = m.entity_id WHERE {' AND '.join(where)}", params).fetchall()

    agg: Dict[int, Dict] = {}
    for eid, root, inv, ts in rows:
        cid = canonical_id(conn, eid)
        a = agg.setdefault(cid, {"roots": {}, "first": ts, "last": ts, "variants": set()})
        a["roots"].setdefault(root, set()).add(inv)
        a["first"] = min(filter(None, [a["first"], ts]), default=None)
        a["last"] = max(filter(None, [a["last"], ts]), default=None)
        if eid != cid:
            a["variants"].add(eid)

    out = []
    for cid, a in agg.items():
        ent = conn.execute("SELECT kind, name FROM entities WHERE id=?", (cid,)).fetchone()
        r, org_id, org_name = role_of(conn, cid, at)
        if role and r != role:
            continue
        counted = [root for root, invs in a["roots"].items()
                   if all_involvements or invs & set(WORKING_INVOLVEMENTS) or (r and INV_LISTING in invs)]
        if not counted:
            continue
        involvements: Dict[str, int] = {}
        for invs in a["roots"].values():
            for inv in invs:
                involvements[inv] = involvements.get(inv, 0) + 1
        out.append({
            "id": cid, "kind": ent[0], "name": ent[1], "role": r, "organization": org_name,
            "sessions": len(counted), "involvement": involvements,
            "first_seen": (a["first"] or "")[:10], "last_seen": (a["last"] or "")[:10],
            "variants": sorted(conn.execute("SELECT name FROM entities WHERE id=?", (v,)).fetchone()[0]
                               for v in a["variants"]),
        })
    out.sort(key=lambda o: (-o["sessions"], o["name"]))
    return out


def pending(conn, limit: int = 200) -> Dict:
    """What needs a human: unconfirmed edges, and mentioned entities with no role."""
    ensure_schema(conn)
    edges = [dict(zip(("id", "src", "relation", "dst", "valid_from", "valid_to", "source", "note"), r))
             for r in conn.execute(
                 "SELECT x.id, s.kind || ':' || s.name, x.relation, d.kind || ':' || d.name, x.valid_from, "
                 "x.valid_to, x.source, x.note FROM entity_edges x JOIN entities s ON s.id = x.src_id "
                 "JOIN entities d ON d.id = x.dst_id WHERE x.confirmed = 0 ORDER BY x.relation, s.name LIMIT ?",
                 (limit,))]
    mentioned = [r[0] for r in conn.execute("SELECT DISTINCT entity_id FROM entity_mentions")]
    no_role = {KIND_PERSON: [], KIND_ORG: []}
    for eid in {canonical_id(conn, e) for e in mentioned}:
        kind, name = conn.execute("SELECT kind, name FROM entities WHERE id=?", (eid,)).fetchone()
        if kind in no_role and role_of(conn, eid)[0] is None:
            no_role[kind].append(name)
    return {"unconfirmed_edges": edges, "people_without_role": sorted(no_role[KIND_PERSON]),
            "organizations_without_role": sorted(no_role[KIND_ORG])}


def confirm_edge(conn, edge_id: int, valid_from: Optional[str] = None, valid_to: Optional[str] = None,
                 reject: bool = False) -> Dict:
    row = conn.execute("SELECT id, confirmed FROM entity_edges WHERE id=?", (edge_id,)).fetchone()
    if not row:
        raise ValueError(f"no edge with id {edge_id}")
    if reject:
        if row[1] == 1:
            raise ValueError(f"edge {edge_id} is confirmed: close it with --valid-to instead of deleting history")
        # Kept with confirmed = -1 instead of deleted: a rebuild re-runs the proposal rules,
        # and a deleted proposal would come back every time.
        conn.execute("UPDATE entity_edges SET confirmed=-1, updated_at=? WHERE id=?", (_now(), edge_id))
        conn.commit()
        return {"id": edge_id, "rejected": True}
    conn.execute("UPDATE entity_edges SET confirmed=1, valid_from=COALESCE(?, valid_from), "
                 "valid_to=COALESCE(?, valid_to), source=CASE WHEN source LIKE 'rule:%' THEN source || '+manual' "
                 "ELSE source END, updated_at=? WHERE id=?", (valid_from, valid_to, _now(), edge_id))
    conn.commit()
    return {"id": edge_id, "confirmed": True}
