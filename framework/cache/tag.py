"""Tag-template rendering for the cache invalidator.

Substitutes `${args.X}` placeholders with str(args[X]). Used by
framework.cache.invalidator.Invalidator.after_mutation when the strategy is
"tag" - the per-mutation tag templates are rendered against the mutation's
GraphQL args and the resulting strings are passed to backend.del_by_tag.

This is a pure string operation - no SQL semantics, no allowlist, no
identifier rewriting (those live in framework/dsl/loader.py for where/set
templates). The DSL loader has already validated that every ${args.X} in
a tag template references an actual GraphQL argument; this function trusts
that contract and raises ValueError only if invoked with an args dict that
omits a referenced name (a runtime, not config, error).
"""

import re
from collections.abc import Mapping
from typing import Any

# Same regex shape as the DSL loader's _ARG_RE (loader.py), but applied for
# literal interpolation rather than SQL-binding.
_ARG_RE = re.compile(r"\$\{args\.([A-Za-z_][A-Za-z0-9_]*)\}")


def render_template(tpl: str, args: Mapping[str, Any]) -> str:
    """Return tpl with every ${args.X} replaced by str(args[X]).

    Raises ValueError if a referenced arg name is not in `args`.
    """
    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in args:
            raise ValueError(f"tag template references unresolved arg '{name}'")
        return str(args[name])

    return _ARG_RE.sub(_sub, tpl)
