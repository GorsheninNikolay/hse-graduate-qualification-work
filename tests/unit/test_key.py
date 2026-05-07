"""Unit tests for framework.cache.key.make_key.

Contract per mvp-roadmap.md line 35: key = sha256(operation_name + sorted_args_json).
The cache key is computed from the RAW GraphQL args (Q2): str "1" must hash
differently from int 1 because the str -> int coercion in
framework/graphql/server.py runs only on the cache-MISS path.
"""

from __future__ import annotations

from framework.cache.key import make_key


def test_determinism() -> None:
    a = make_key("getUser", {"id": "1"})
    b = make_key("getUser", {"id": "1"})
    assert a == b
    # Sanity: sha256 hex digest is 64 chars.
    assert len(a) == 64


def test_arg_order_invariance() -> None:
    a = make_key("q", {"a": 1, "b": 2})
    b = make_key("q", {"b": 2, "a": 1})
    assert a == b


def test_op_distinct_and_arg_distinct() -> None:
    # Different operation names produce different keys.
    assert make_key("getUser", {"id": "1"}) != make_key("getTeam", {"id": "1"})
    # Same operation, different args -> different keys.
    assert make_key("getUser", {"id": "1"}) != make_key("getUser", {"id": "2"})
    # Q2 contract: raw vs coerced (str "1" vs int 1) hash differently.
    assert make_key("getUser", {"id": "1"}) != make_key("getUser", {"id": 1})
