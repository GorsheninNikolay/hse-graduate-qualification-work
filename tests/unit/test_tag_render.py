"""Unit tests for framework.cache.tag.render_template.

Pure string substitution: every ${args.X} is replaced by str(args[X]).
Raises ValueError when a referenced arg name is absent from the args mapping.
"""

from __future__ import annotations

import pytest

from framework.cache.tag import render_template


def test_substitutes_args() -> None:
    # Single substitution.
    assert render_template("user:${args.id}", {"id": 42}) == "user:42"
    # Multi-substitution.
    assert (
        render_template(
            "team:${args.teamId}:user:${args.id}", {"teamId": 7, "id": 42}
        )
        == "team:7:user:42"
    )
    # No-placeholder template returns unchanged.
    assert render_template("static-tag", {"id": 1}) == "static-tag"


def test_unresolved_arg_raises_value_error() -> None:
    with pytest.raises(ValueError) as exc_info:
        render_template("user:${args.missing}", {"id": 1})
    # Error message names the unresolved placeholder.
    assert "missing" in str(exc_info.value)
