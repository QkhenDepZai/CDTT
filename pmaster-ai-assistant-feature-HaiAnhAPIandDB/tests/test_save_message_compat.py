import pymysql

import database


class FakeCursor:
    def __init__(self, fail_new_columns):
        self.fail_new_columns = fail_new_columns
        self.executed = []

    def execute(self, sql, params):
        if self.fail_new_columns and "answer_status" in sql:
            raise pymysql.err.OperationalError(1054, "Unknown column 'answer_status'")
        self.executed.append((sql, params))


class FakeConnection:
    def commit(self):
        pass


def test_save_message_writes_rag_columns(monkeypatch):
    monkeypatch.setattr(database, "_messages_has_rag_columns", True)
    cursor = FakeCursor(fail_new_columns=False)
    database.save_message(cursor, FakeConnection(), 1, "model", "hi",
                          answer_status="answered", retrieved_chunk_ids=[3, 4], latency_ms=120)
    sql, params = cursor.executed[0]
    assert "retrieved_chunk_ids" in sql
    assert params[-3:] == ("answered", "[3, 4]", 120)


def test_save_message_falls_back_on_database_without_migration(monkeypatch):
    monkeypatch.setattr(database, "_messages_has_rag_columns", True)
    cursor = FakeCursor(fail_new_columns=True)
    database.save_message(cursor, FakeConnection(), 1, "model", "hi", answer_status="answered")
    assert len(cursor.executed) == 1 and "answer_status" not in cursor.executed[0][0]
    assert database._messages_has_rag_columns is False
