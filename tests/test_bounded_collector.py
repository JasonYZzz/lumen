"""Unit tests for BoundedCollector (command output head/tail bounding)."""

from lumen.tools.capability import MAX_OUTPUT_BYTES, BoundedCollector


def test_small_output_kept_verbatim_not_truncated() -> None:
    collector = BoundedCollector()
    collector.feed(b"hello world")
    assert collector.truncated is False
    assert collector.total_bytes == 11
    assert collector.render() == "hello world"


def test_output_under_cap_not_truncated() -> None:
    collector = BoundedCollector()
    collector.feed(b"x" * MAX_OUTPUT_BYTES)
    assert collector.truncated is False
    assert collector.total_bytes == MAX_OUTPUT_BYTES
    assert collector.render() == "x" * MAX_OUTPUT_BYTES


def test_output_just_over_cap_truncates_keeps_head_and_tail() -> None:
    collector = BoundedCollector()
    # Head fills the cap, then one extra byte triggers truncation + tail.
    collector.feed(b"H" * MAX_OUTPUT_BYTES)
    collector.feed(b"T")
    assert collector.truncated is True
    assert collector.total_bytes == MAX_OUTPUT_BYTES + 1
    rendered = collector.render()
    assert rendered.startswith("H" * MAX_OUTPUT_BYTES)
    assert "truncated" in rendered
    assert rendered.endswith("T")


def test_large_output_drops_middle_keeps_head_and_tail() -> None:
    collector = BoundedCollector()
    # A distinct head marker, a huge middle that will be dropped, and a
    # distinct tail marker that must survive in the rolling tail.
    collector.feed(b"<<HEAD>>")
    collector.feed(b"M" * (MAX_OUTPUT_BYTES * 4))
    collector.feed(b"<<TAIL>>")
    rendered = collector.render()
    assert collector.truncated is True
    assert collector.total_bytes == 8 + MAX_OUTPUT_BYTES * 4 + 8
    assert rendered.startswith("<<HEAD>>")
    assert rendered.endswith("<<TAIL>>")


def test_incremental_feeds_accumulate_tail_rolling() -> None:
    """Feeding many small chunks keeps only the rolling tail once total
    output exceeds the head cap."""
    collector = BoundedCollector()
    collector.feed(b"A" * 1000)
    # Push enough B's to exceed the cap so truncation engages and the tail
    # rolls to the most recent bytes.
    for _ in range(MAX_OUTPUT_BYTES + 500):
        collector.feed(b"B")
    rendered = collector.render()
    assert collector.truncated is True
    assert rendered.startswith("A" * 1000)
    # Tail holds the most recent B's.
    assert rendered.endswith("B")


def test_total_bytes_counts_all_input() -> None:
    collector = BoundedCollector()
    collector.feed(b"abc")
    collector.feed(b"defgh")
    assert collector.total_bytes == 8
