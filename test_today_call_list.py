"""Today's call list: replies first, then new work and follow-ups in turn.

The bug this pins down is a starvation bug. The list used to take one bucket at
a time -- replies, then every overdue follow-up, then, only if there was room
left, leads nobody had ever called. With a few dozen follow-ups there never was
room, so the owner could work the list all day without moving a single lead out
of ``new``. The funnel said "Contacted 97" and stayed there.

No database: the selection is a pure function over grouped rows.
"""

from bizstack_leads import lifecycle, queue


def _lead(lead_id, action, **extra):
    row = {"id": lead_id, "name": f"Lead {lead_id}", "next_action": action}
    row.update(extra)
    return row


def _grouped(*leads):
    return queue.group(leads)


def test_replies_are_taken_before_anything_else():
    grouped = _grouped(
        _lead(1, lifecycle.ACTION_FOLLOW_UP),
        _lead(2, lifecycle.ACTION_RESPOND),
        _lead(3, lifecycle.ACTION_FIRST_TOUCH),
    )
    assert [r["id"] for r in queue.call_candidates(grouped, limit=3)] == [2, 3, 1]


def test_a_long_follow_up_list_cannot_hide_never_contacted_leads():
    leads = [_lead(i, lifecycle.ACTION_FIRST_TOUCH) for i in range(1, 6)]
    leads += [_lead(100 + i, lifecycle.ACTION_FOLLOW_UP) for i in range(60)]
    calls = queue.call_candidates(_grouped(*leads), limit=12)
    ids = [r["id"] for r in calls]
    # All five new leads appear, even though there are sixty overdue follow-ups.
    assert {1, 2, 3, 4, 5} <= set(ids)
    assert len(calls) == 12
    # And they are spread through the list, not appended after the follow-ups.
    assert ids.index(1) < ids.index(100)


def test_the_two_buckets_take_turns():
    grouped = _grouped(
        *[_lead(i, lifecycle.ACTION_FIRST_TOUCH) for i in (1, 2, 3)],
        *[_lead(10 + i, lifecycle.ACTION_FOLLOW_UP) for i in (1, 2)],
    )
    assert [r["id"] for r in queue.call_candidates(grouped, limit=5)] == [1, 11, 2, 12, 3]


def test_unreachable_rows_are_skipped_without_consuming_a_slot():
    grouped = _grouped(
        _lead(1, lifecycle.ACTION_RESPOND, phone=""),
        _lead(2, lifecycle.ACTION_FIRST_TOUCH, phone="7575550101"),
        _lead(3, lifecycle.ACTION_FOLLOW_UP, phone="7575550102"),
    )
    calls = queue.call_candidates(
        grouped, limit=2, keep=lambda lead: bool(lead.get("phone")),
    )
    assert [r["id"] for r in calls] == [2, 3]


def test_the_limit_is_respected_even_when_one_bucket_is_empty():
    grouped = _grouped(*[_lead(i, lifecycle.ACTION_FOLLOW_UP) for i in range(20)])
    assert len(queue.call_candidates(grouped, limit=4)) == 4
