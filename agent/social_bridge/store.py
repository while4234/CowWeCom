"""SQLite-backed domain storage for social bridge state.

The bridge layer intentionally has no dependency on LLM execution or channel
sending. It owns durable user/message/relationship state and small relation
memory files that later workers can consume.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory.config import get_default_memory_config


_DDL = """
CREATE TABLE IF NOT EXISTS bridge_users (
    actor_user_id  TEXT PRIMARY KEY,
    memory_user_id TEXT NOT NULL UNIQUE,
    display_name   TEXT NOT NULL DEFAULT '',
    metadata       TEXT NOT NULL DEFAULT '{}',
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bridge_relationships (
    pair_id              TEXT PRIMARY KEY,
    actor_user_id        TEXT NOT NULL,
    target_actor_user_id TEXT NOT NULL,
    actor_memory_user_id TEXT NOT NULL,
    target_memory_user_id TEXT NOT NULL,
    relation_text        TEXT NOT NULL,
    created_at           INTEGER NOT NULL,
    updated_at           INTEGER NOT NULL,
    UNIQUE (actor_user_id, target_actor_user_id)
);

CREATE TABLE IF NOT EXISTS bridge_messages (
    message_id           TEXT PRIMARY KEY,
    pair_id              TEXT NOT NULL,
    sender_actor_user_id TEXT NOT NULL,
    target_actor_user_id TEXT NOT NULL,
    body                 TEXT NOT NULL,
    status               TEXT NOT NULL,
    result               TEXT NOT NULL DEFAULT '{}',
    created_at           INTEGER NOT NULL,
    updated_at           INTEGER NOT NULL,
    sent_at              INTEGER
);

CREATE TABLE IF NOT EXISTS bridge_audit (
    audit_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id TEXT NOT NULL DEFAULT '',
    action        TEXT NOT NULL,
    details       TEXT NOT NULL DEFAULT '{}',
    created_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bridge_users_updated
    ON bridge_users (updated_at);

CREATE INDEX IF NOT EXISTS idx_bridge_relationships_actor
    ON bridge_relationships (actor_user_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_bridge_relationships_target
    ON bridge_relationships (target_actor_user_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_bridge_messages_target_status
    ON bridge_messages (target_actor_user_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_bridge_messages_sender_status
    ON bridge_messages (sender_actor_user_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_bridge_audit_actor_time
    ON bridge_audit (actor_user_id, created_at);
"""

PENDING_STATUS = "pending"
SENT_STATUS = "sent"


@dataclass(frozen=True)
class BridgeUser:
    actor_user_id: str
    memory_user_id: str
    display_name: str = ""
    metadata: Optional[Dict[str, Any]] = None
    created_at: int = 0
    updated_at: int = 0


@dataclass(frozen=True)
class BridgeRelationship:
    pair_id: str
    actor_user_id: str
    target_actor_user_id: str
    actor_memory_user_id: str
    target_memory_user_id: str
    relation_text: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class BridgeMessage:
    message_id: str
    pair_id: str
    sender_actor_user_id: str
    target_actor_user_id: str
    body: str
    status: str
    result: Dict[str, Any]
    created_at: int
    updated_at: int
    sent_at: Optional[int] = None


@dataclass(frozen=True)
class PendingBridgeMessage:
    message: BridgeMessage
    sender: BridgeUser
    relationship: Optional[BridgeRelationship]


@dataclass(frozen=True)
class BridgeAuditEntry:
    audit_id: int
    actor_user_id: str
    action: str
    details: Dict[str, Any]
    created_at: int


def compute_pair_id(memory_user_id: str, other_memory_user_id: str) -> str:
    """Return a stable pair id independent of user order."""
    first = _require_text(memory_user_id, "memory_user_id")
    second = _require_text(other_memory_user_id, "other_memory_user_id")
    ordered = sorted([first, second])
    digest = hashlib.sha256("\n".join(ordered).encode("utf-8")).hexdigest()[:16]
    return f"pair_{digest}"


class BridgeStore:
    """Thread-safe SQLite store for bridge users, relationships, and messages."""

    def __init__(self, db_path: Optional[Path] = None, workspace_root: Optional[Path] = None):
        config = get_default_memory_config()
        self._db_path = Path(db_path) if db_path else config.get_db_path()
        self._workspace_root = Path(workspace_root) if workspace_root else config.get_workspace()
        self._lock = threading.Lock()
        self._init_db()

    def register_user(
        self,
        actor_user_id: str,
        memory_user_id: str,
        display_name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> BridgeUser:
        actor_id = _require_text(actor_user_id, "actor_user_id")
        memory_id = _require_text(memory_user_id, "memory_user_id")
        clean_metadata = _json_dict(metadata, "metadata")
        now = _now()

        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO bridge_users
                            (actor_user_id, memory_user_id, display_name, metadata, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(actor_user_id) DO UPDATE SET
                            memory_user_id = excluded.memory_user_id,
                            display_name = excluded.display_name,
                            metadata = excluded.metadata,
                            updated_at = excluded.updated_at
                        """,
                        (
                            actor_id,
                            memory_id,
                            display_name or "",
                            json.dumps(clean_metadata, ensure_ascii=False),
                            now,
                            now,
                        ),
                    )
                    self._insert_audit(conn, actor_id, "register_user", {"memory_user_id": memory_id})
                    row = self._fetch_user_row(conn, actor_id)
            finally:
                conn.close()

        return self._row_to_user(row)

    def list_visible_users(self, exclude_actor_id: str, limit: int = 20) -> List[BridgeUser]:
        exclude_id = _require_text(exclude_actor_id, "exclude_actor_id")
        safe_limit = max(1, int(limit))

        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM bridge_users
                    WHERE actor_user_id != ?
                    ORDER BY updated_at DESC, actor_user_id ASC
                    LIMIT ?
                    """,
                    (exclude_id, safe_limit),
                ).fetchall()
            finally:
                conn.close()

        return [self._row_to_user(row) for row in rows]

    def get_user(self, actor_user_id: str) -> Optional[BridgeUser]:
        actor_id = _require_text(actor_user_id, "actor_user_id")
        with self._lock:
            conn = self._connect()
            try:
                row = self._fetch_user_row(conn, actor_id)
            finally:
                conn.close()
        return self._row_to_user(row) if row is not None else None

    def set_relationship(
        self,
        actor_user_id: str,
        target_actor_user_id: str,
        relation_text: str,
    ) -> BridgeRelationship:
        actor_id = _require_text(actor_user_id, "actor_user_id")
        target_id = _require_text(target_actor_user_id, "target_actor_user_id")
        if actor_id == target_id:
            raise ValueError("target_actor_user_id must differ from actor_user_id")
        text = _require_text(relation_text, "relation_text")
        now = _now()

        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    actor = self._require_user(conn, actor_id)
                    target = self._require_user(conn, target_id)
                    pair_id = compute_pair_id(actor["memory_user_id"], target["memory_user_id"])
                    existing = self._fetch_relationship_by_pair_id(conn, pair_id)
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO bridge_relationships
                                (
                                    pair_id, actor_user_id, target_actor_user_id,
                                    actor_memory_user_id, target_memory_user_id,
                                    relation_text, created_at, updated_at
                                )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                pair_id,
                                actor_id,
                                target_id,
                                actor["memory_user_id"],
                                target["memory_user_id"],
                                text,
                                now,
                                now,
                            ),
                        )
                    else:
                        relation_text = self._merge_relationship_text(
                            existing,
                            actor_id,
                            target_id,
                            text,
                        )
                        conn.execute(
                            """
                            UPDATE bridge_relationships
                            SET actor_user_id = ?,
                                target_actor_user_id = ?,
                                actor_memory_user_id = ?,
                                target_memory_user_id = ?,
                                relation_text = ?,
                                updated_at = ?
                            WHERE pair_id = ?
                            """,
                            (
                                actor_id,
                                target_id,
                                actor["memory_user_id"],
                                target["memory_user_id"],
                                relation_text,
                                now,
                                pair_id,
                            ),
                        )
                    self._append_relationship_memory(pair_id, actor_id, target_id, text, now)
                    self._insert_audit(
                        conn,
                        actor_id,
                        "set_relationship",
                        {"target_actor_user_id": target_id, "pair_id": pair_id},
                    )
                    row = self._fetch_relationship_by_pair_id(conn, pair_id)
            finally:
                conn.close()

        return self._row_to_relationship(row)

    def get_relationship(
        self,
        actor_user_id: str,
        target_actor_user_id: str,
    ) -> Optional[BridgeRelationship]:
        actor_id = _require_text(actor_user_id, "actor_user_id")
        target_id = _require_text(target_actor_user_id, "target_actor_user_id")

        with self._lock:
            conn = self._connect()
            try:
                actor = self._fetch_user_row(conn, actor_id)
                target = self._fetch_user_row(conn, target_id)
                if actor is None or target is None:
                    row = None
                else:
                    pair_id = compute_pair_id(actor["memory_user_id"], target["memory_user_id"])
                    row = self._fetch_relationship_by_pair_id(conn, pair_id)
            finally:
                conn.close()

        return self._row_to_relationship(row) if row is not None else None

    def create_bridge_message(
        self,
        sender_actor_user_id: str,
        target_actor_user_id: str,
        body: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> BridgeMessage:
        sender_id = _require_text(sender_actor_user_id, "sender_actor_user_id")
        target_id = _require_text(target_actor_user_id, "target_actor_user_id")
        if sender_id == target_id:
            raise ValueError("target_actor_user_id must differ from sender_actor_user_id")
        clean_body = _require_text(body, "body")
        clean_result = _json_dict(result, "result")
        now = _now()

        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    sender = self._require_user(conn, sender_id)
                    target = self._require_user(conn, target_id)
                    pair_id = compute_pair_id(sender["memory_user_id"], target["memory_user_id"])
                    message_id = self._new_message_id(sender_id, target_id, clean_body, now)
                    conn.execute(
                        """
                        INSERT INTO bridge_messages
                            (
                                message_id, pair_id, sender_actor_user_id,
                                target_actor_user_id, body, status, result,
                                created_at, updated_at, sent_at
                            )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            message_id,
                            pair_id,
                            sender_id,
                            target_id,
                            clean_body,
                            PENDING_STATUS,
                            json.dumps(clean_result, ensure_ascii=False),
                            now,
                            now,
                        ),
                    )
                    self._append_message_memory(pair_id, sender_id, target_id, clean_body, now)
                    self._insert_audit(
                        conn,
                        sender_id,
                        "create_bridge_message",
                        {"target_actor_user_id": target_id, "message_id": message_id, "pair_id": pair_id},
                    )
                    row = self._fetch_message_row(conn, message_id)
            finally:
                conn.close()

        return self._row_to_message(row)

    def persist_bridge_message(
        self,
        sender_actor_user_id: str,
        target_actor_user_id: str,
        body: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> BridgeMessage:
        """Alias for callers that describe message creation as persistence."""
        return self.create_bridge_message(sender_actor_user_id, target_actor_user_id, body, result)

    def mark_sent(
        self,
        message_id: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> Optional[BridgeMessage]:
        return self._mark_message_status(message_id, SENT_STATUS, result, sent=True)

    def mark_pending(
        self,
        message_id: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> Optional[BridgeMessage]:
        return self._mark_message_status(message_id, PENDING_STATUS, result, sent=False)

    def get_message(self, message_id: str) -> Optional[BridgeMessage]:
        clean_id = _require_text(message_id, "message_id")
        with self._lock:
            conn = self._connect()
            try:
                row = self._fetch_message_row(conn, clean_id)
            finally:
                conn.close()
        return self._row_to_message(row) if row is not None else None

    def list_pending_for_actor(self, actor_user_id: str, limit: int = 20) -> List[PendingBridgeMessage]:
        actor_id = _require_text(actor_user_id, "actor_user_id")
        safe_limit = max(1, int(limit))

        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM bridge_messages
                    WHERE (target_actor_user_id = ? OR sender_actor_user_id = ?) AND status = ?
                    ORDER BY created_at ASC, message_id ASC
                    LIMIT ?
                    """,
                    (actor_id, actor_id, PENDING_STATUS, safe_limit),
                ).fetchall()
                pending = []
                for row in rows:
                    message = self._row_to_message(row)
                    sender_row = self._fetch_user_row(conn, message.sender_actor_user_id)
                    relationship_row = self._fetch_relationship_by_pair_id(conn, message.pair_id)
                    pending.append(
                        PendingBridgeMessage(
                            message=message,
                            sender=self._row_to_user(sender_row),
                            relationship=(
                                self._row_to_relationship(relationship_row)
                                if relationship_row is not None
                                else None
                            ),
                        )
                    )
            finally:
                conn.close()

        return pending

    def audit(
        self,
        actor_user_id: str,
        action: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> BridgeAuditEntry:
        actor_id = actor_user_id.strip() if actor_user_id else ""
        clean_action = _require_text(action, "action")
        clean_details = _json_dict(details, "details")

        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    audit_id = self._insert_audit(conn, actor_id, clean_action, clean_details)
                    row = conn.execute(
                        "SELECT * FROM bridge_audit WHERE audit_id = ?",
                        (audit_id,),
                    ).fetchone()
            finally:
                conn.close()

        return self._row_to_audit(row)

    @staticmethod
    def dto_to_dict(value: Any) -> Dict[str, Any]:
        """Convert a DTO into a JSON-serializable dictionary."""
        return asdict(value)

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_DDL)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _mark_message_status(
        self,
        message_id: str,
        status: str,
        result: Optional[Dict[str, Any]],
        sent: bool,
    ) -> Optional[BridgeMessage]:
        clean_id = _require_text(message_id, "message_id")
        clean_result = _json_dict(result, "result") if result is not None else None
        now = _now()

        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    current = self._fetch_message_row(conn, clean_id)
                    if current is None:
                        return None
                    merged_result = (
                        clean_result
                        if clean_result is not None
                        else json.loads(current["result"] or "{}")
                    )
                    conn.execute(
                        """
                        UPDATE bridge_messages
                        SET status = ?, result = ?, updated_at = ?, sent_at = ?
                        WHERE message_id = ?
                        """,
                        (
                            status,
                            json.dumps(merged_result, ensure_ascii=False),
                            now,
                            now if sent else None,
                            clean_id,
                        ),
                    )
                    self._insert_audit(
                        conn,
                        current["sender_actor_user_id"],
                        f"mark_{status}",
                        {"message_id": clean_id, "target_actor_user_id": current["target_actor_user_id"]},
                    )
                    row = self._fetch_message_row(conn, clean_id)
            finally:
                conn.close()

        return self._row_to_message(row)

    def _require_user(self, conn: sqlite3.Connection, actor_user_id: str) -> sqlite3.Row:
        row = self._fetch_user_row(conn, actor_user_id)
        if row is None:
            raise ValueError(f"Bridge user not registered: {actor_user_id}")
        return row

    @staticmethod
    def _fetch_user_row(conn: sqlite3.Connection, actor_user_id: str) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM bridge_users WHERE actor_user_id = ?",
            (actor_user_id,),
        ).fetchone()

    @staticmethod
    def _fetch_relationship_row(
        conn: sqlite3.Connection,
        actor_user_id: str,
        target_actor_user_id: str,
    ) -> Optional[sqlite3.Row]:
        return conn.execute(
            """
            SELECT *
            FROM bridge_relationships
            WHERE actor_user_id = ? AND target_actor_user_id = ?
            """,
            (actor_user_id, target_actor_user_id),
        ).fetchone()

    @staticmethod
    def _fetch_relationship_by_pair_id(
        conn: sqlite3.Connection,
        pair_id: str,
    ) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM bridge_relationships WHERE pair_id = ?",
            (pair_id,),
        ).fetchone()

    @staticmethod
    def _fetch_message_row(conn: sqlite3.Connection, message_id: str) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM bridge_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()

    @staticmethod
    def _insert_audit(
        conn: sqlite3.Connection,
        actor_user_id: str,
        action: str,
        details: Dict[str, Any],
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO bridge_audit (actor_user_id, action, details, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                actor_user_id or "",
                action,
                json.dumps(details, ensure_ascii=False),
                _now(),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _merge_relationship_text(
        existing: sqlite3.Row,
        actor_user_id: str,
        target_actor_user_id: str,
        relation_text: str,
    ) -> str:
        current = str(existing["relation_text"] or "").strip()
        new_line = f"{actor_user_id} -> {target_actor_user_id}: {relation_text}"
        if not current:
            return new_line
        if relation_text in current or new_line in current:
            return current
        return f"{current}\n{new_line}"

    def _append_relationship_memory(
        self,
        pair_id: str,
        actor_user_id: str,
        target_actor_user_id: str,
        relation_text: str,
        timestamp: int,
    ) -> None:
        line = (
            f"- {_format_timestamp(timestamp)} relationship "
            f"{actor_user_id} -> {target_actor_user_id}: {relation_text}"
        )
        self._append_relation_line(pair_id, line, timestamp)

    def _append_message_memory(
        self,
        pair_id: str,
        sender_actor_user_id: str,
        target_actor_user_id: str,
        body: str,
        timestamp: int,
    ) -> None:
        line = (
            f"- {_format_timestamp(timestamp)} message "
            f"{sender_actor_user_id} -> {target_actor_user_id}: {body}"
        )
        self._append_relation_line(pair_id, line, timestamp)

    def _append_relation_line(self, pair_id: str, line: str, timestamp: int) -> None:
        relation_dir = self._workspace_root / "memory" / "relations" / pair_id
        relation_dir.mkdir(parents=True, exist_ok=True)
        for path in (relation_dir / "MEMORY.md", relation_dir / f"{date.fromtimestamp(timestamp).isoformat()}.md"):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    @staticmethod
    def _new_message_id(
        sender_actor_user_id: str,
        target_actor_user_id: str,
        body: str,
        timestamp: int,
    ) -> str:
        seed = f"{sender_actor_user_id}\n{target_actor_user_id}\n{timestamp}\n{time.time_ns()}\n{body}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        return f"bridge_msg_{digest}"

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> BridgeUser:
        return BridgeUser(
            actor_user_id=row["actor_user_id"],
            memory_user_id=row["memory_user_id"],
            display_name=row["display_name"],
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_relationship(row: sqlite3.Row) -> BridgeRelationship:
        return BridgeRelationship(
            pair_id=row["pair_id"],
            actor_user_id=row["actor_user_id"],
            target_actor_user_id=row["target_actor_user_id"],
            actor_memory_user_id=row["actor_memory_user_id"],
            target_memory_user_id=row["target_memory_user_id"],
            relation_text=row["relation_text"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> BridgeMessage:
        return BridgeMessage(
            message_id=row["message_id"],
            pair_id=row["pair_id"],
            sender_actor_user_id=row["sender_actor_user_id"],
            target_actor_user_id=row["target_actor_user_id"],
            body=row["body"],
            status=row["status"],
            result=json.loads(row["result"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            sent_at=row["sent_at"],
        )

    @staticmethod
    def _row_to_audit(row: sqlite3.Row) -> BridgeAuditEntry:
        return BridgeAuditEntry(
            audit_id=row["audit_id"],
            actor_user_id=row["actor_user_id"],
            action=row["action"],
            details=json.loads(row["details"] or "{}"),
            created_at=row["created_at"],
        )


_store_instance: Optional[BridgeStore] = None
_store_lock = threading.Lock()


def get_bridge_store() -> BridgeStore:
    """Return the process-wide bridge store singleton."""
    global _store_instance
    if _store_instance is not None:
        return _store_instance

    with _store_lock:
        if _store_instance is None:
            _store_instance = BridgeStore()
        return _store_instance


def _require_text(value: str, field_name: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _json_dict(value: Optional[Dict[str, Any]], field_name: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    json.dumps(value, ensure_ascii=False)
    return value


def _now() -> int:
    return int(time.time())


def _format_timestamp(timestamp: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
