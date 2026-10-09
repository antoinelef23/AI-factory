from worker import MemoryInbox, Message, handle, run


def test_every_message_is_handled_then_acknowledged():
    inbox = MemoryInbox([Message("orders", {"id": 1}), Message("orders", {"id": 2})])
    assert run(inbox) == 2
    assert [m.payload["id"] for m in inbox.acked] == [1, 2] and inbox.receive() is None


def test_a_limit_stops_the_loop_and_leaves_the_rest_pending():
    inbox = MemoryInbox([Message("a"), Message("b"), Message("c")])
    assert run(inbox, limit=2) == 2 and len(inbox.pending) == 1


def test_handle_reports_the_topic():
    assert handle(Message("orders")) == {"status": "ok", "topic": "orders"}
