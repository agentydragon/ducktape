local I = import '../../specimens/lib.libsonnet';

// iss-012: ChatStorePersisted should use SQLAlchemy ORM instead of raw SQL

I.issueOneOccurrence(
  rationale=|||
    The `ChatStorePersisted` class in `adgn/src/adgn/mcp/chat/server.py` uses raw aiosqlite queries
    instead of SQLAlchemy ORM, making it inconsistent with the rest of the persistence layer.

    **Current state:**
    The ChatStorePersisted class (lines 171-283) uses raw SQL queries via `self._persistence._open()`:

    **Line 182-189 (last_id_async):**
    ```python
    async with (
        self._persistence._open() as db,
        db.execute("SELECT MAX(id) AS last_id FROM chat_messages WHERE agent_id = ?", (self._agent,)) as cur,
    ):
        if (row := await cur.fetchone()) and (val := row["last_id"]) is not None:
            return str(val)
        return None
    ```

    **Line 191-200 (get_last_read_async):**
    ```python
    async with (
        self._persistence._open() as db,
        db.execute(
            "SELECT last_id FROM chat_last_read WHERE agent_id = ? AND server_name = ?", (self._agent, server_name)
        ) as cur,
    ):
        if (row := await cur.fetchone()) and (val := row["last_id"]) is not None:
            return str(val)
        return None
    ```

    **Line 202-213 (append):**
    ```python
    async with self._persistence._open() as db:
        cur = await db.execute(
            "INSERT INTO chat_messages (agent_id, ts, author, mime, content) VALUES (?, ?, ?, ?, ?)",
            (self._agent, ts, author.value, mime, content),
        )
        await db.commit()
        new_id = str(cur.lastrowid)
    ```

    **Line 215-229 (get_message_async):**
    ```python
    async with (
        self._persistence._open() as db,
        db.execute(
            "SELECT id, ts, author, mime, content FROM chat_messages WHERE agent_id = ? AND id = ?",
            (self._agent, seq),
        ) as cur,
    ):
        if not (row := await cur.fetchone()):
            return None
        return _row_to_message(row)
    ```

    **Line 237-283 (read_pending_and_advance):**
    ```python
    # Multiple raw SQL queries with manual transaction handling
    async with (
        self._persistence._open() as db,
        db.execute(
            "SELECT last_id FROM chat_last_read WHERE agent_id = ? AND server_name = ?", (self._agent, server_name)
        ) as cur,
    ):
        r = await cur.fetchone()
        after_seq = r["last_id"] if r else None

    # ... more raw SQL queries for fetching messages and updating last_read
    ```

    **Why this is problematic:**

    1. **Inconsistent with codebase patterns**: The rest of the persistence layer uses SQLAlchemy ORM:
       ```python
       async with self._session() as session:
           result = await session.execute(select(Agent).where(Agent.id == agent_id))
           agent = result.scalar_one_or_none()
       ```

    2. **ORM models already exist**: `ChatMessage` and `ChatLastRead` models are defined in
       `adgn/src/adgn/agent/persist/models.py` (lines 197-225) but not being used.

    3. **Manual row parsing**: Uses `_row_to_message(row: Row)` converter (line 29) instead of
       automatic ORM object mapping.

    4. **Type safety**: Raw SQL with string-based queries is more error-prone than ORM with
       type-checked model attributes.

    5. **Maintenance**: SQL schema changes require manual updates to query strings and row
       parsers, while ORM models provide a single source of truth.

    6. **Raw database access**: Uses `_persistence._open()` (private method) instead of the
       proper `_session()` async context manager.

    **Recommended fix:**

    Refactor ChatStorePersisted to use SQLAlchemy ORM:

    **Example - last_id_async:**
    ```python
    async def last_id_async(self) -> str | None:
        async with self._persistence._session() as session:
            from sqlalchemy import select, func
            from adgn.agent.persist.models import ChatMessage

            result = await session.execute(
                select(func.max(ChatMessage.id)).where(ChatMessage.agent_id == self._agent)
            )
            max_id = result.scalar_one_or_none()
            return str(max_id) if max_id is not None else None
    ```

    **Example - get_last_read_async:**
    ```python
    async def get_last_read_async(self, server_name: str) -> str | None:
        async with self._persistence._session() as session:
            from sqlalchemy import select
            from adgn.agent.persist.models import ChatLastRead

            result = await session.execute(
                select(ChatLastRead.last_id).where(
                    ChatLastRead.agent_id == self._agent,
                    ChatLastRead.server_name == server_name
                )
            )
            last_id = result.scalar_one_or_none()
            return str(last_id) if last_id is not None else None
    ```

    **Example - append:**
    ```python
    async def append(self, *, author: ChatAuthor, mime: str, content: str) -> str:
        from adgn.agent.persist.models import ChatMessage

        async with self._persistence._session() as session:
            msg = ChatMessage(
                agent_id=self._agent,
                timestamp=datetime.now(UTC),
                author=author,
                mime=mime,
                content=content
            )
            session.add(msg)
            await session.commit()
            await session.refresh(msg)
            new_id = str(msg.id)

        await self._notify_other_head(author=author)
        return new_id
    ```

    **Example - get_message_async:**
    ```python
    async def get_message_async(self, msg_id: str) -> ChatMessage | None:
        from adgn.agent.persist.models import ChatMessage as ChatMessageModel

        try:
            seq = int(msg_id)
        except (TypeError, ValueError):
            return None

        async with self._persistence._session() as session:
            result = await session.execute(
                select(ChatMessageModel).where(
                    ChatMessageModel.agent_id == self._agent,
                    ChatMessageModel.id == seq
                )
            )
            msg_model = result.scalar_one_or_none()
            if not msg_model:
                return None

            # Convert ORM model to Pydantic ChatMessage
            return ChatMessage(
                id=str(msg_model.id),
                ts=msg_model.timestamp.isoformat(),
                author=ChatAuthor(msg_model.author),
                mime=msg_model.mime,
                content=msg_model.content
            )
    ```

    **Benefits:**
    - Consistent with rest of codebase (follows established patterns)
    - Uses existing ORM models (single source of truth for schema)
    - Type-safe attribute access instead of string-based column names
    - Automatic schema migration support via Alembic
    - Easier to maintain and refactor
    - Better IDE support (autocomplete, type checking)
    - No manual row parsing needed

    **Note:**
    Line 5 imports `from aiosqlite import Row` and line 29 defines `_row_to_message(row: Row)`.
    These should be removed after migration to ORM, as they're only needed for raw SQL approach.
  |||,
  properties=['architectural-inconsistency', 'maintainability', 'type-safety'],
  filesToRanges={
    'adgn/src/adgn/mcp/chat/server.py': [
      [5, 5],       // Import aiosqlite.Row (should use ORM models instead)
      [29, 37],     // _row_to_message converter (not needed with ORM)
      [171, 283],   // ChatStorePersisted class using raw SQL
      [182, 189],   // last_id_async - raw SELECT MAX query
      [191, 200],   // get_last_read_async - raw SELECT query
      [202, 213],   // append - raw INSERT query
      [215, 229],   // get_message_async - raw SELECT query
      [237, 283],   // read_pending_and_advance - multiple raw SQL queries
    ],
  },
)
