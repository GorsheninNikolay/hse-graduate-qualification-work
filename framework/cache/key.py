"""Cache key derivation for the read path.

contract per mvp-roadmap.md line 35: key = sha256(operation_name +
sorted_args_json). NOT the canonical-selection-set form from ADR-023 / dsl-spec
§5 lines 156-161 - the MVP simplification is intentional.

Q2 from the plan: keys are computed from RAW GraphQL args (as
ariadne parsed them - so an `id: ID!` arg arrives as a Python str). The
str -> int coercion in framework/graphql/server.py:_coerce_args runs only
on the cache-MISS path before the SQL bind; it never sees the cache key.
This keeps the cache key a pure function of the GraphQL request payload,
independent of internal type bridging that may evolve (vkr-272 deferred).
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def make_key(operation_name: str, args: Mapping[str, Any]) -> str:
    """Return a 64-char hex digest unique to (op, args).

    Order-independent in args: {a: 1, b: 2} keys identically to {b: 2, a: 1}
    via sort_keys=True.
    """
    payload = json.dumps(
        {"op": operation_name, "args": dict(args)},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
