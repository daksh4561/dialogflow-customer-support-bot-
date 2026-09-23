import json


def write_audit(cur, session_id, member_id, actor, event_type,
                 request_type=None, decision=None, detail=None):
    """
    Inserts one row into audit_log. The DB trigger (trg_audit_log_hash)
    automatically computes prev_hash / row_hash - we never touch hashing
    from the application layer, which is intentional: the guarantee of
    immutability lives in the database, not in app code that could be
    bypassed.

    `cur` must be an open psycopg cursor inside an active transaction.
    Caller commits/rolls back.
    """
    cur.execute(
        """
        INSERT INTO audit_log
            (session_id, member_id, actor, event_type, request_type, decision, detail)
        VALUES
            (%s, %s, %s, %s, %s, %s, %s::jsonb)
        RETURNING audit_id, row_hash
        """,
        (
            session_id,
            member_id,
            actor,
            event_type,
            request_type,
            decision,
            json.dumps(detail or {}),
        ),
    )
    return cur.fetchone()
