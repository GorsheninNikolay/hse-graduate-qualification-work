"""Resolver-side errors that surface as GraphQL errors[] entries."""

from typing import Any

from graphql import GraphQLError


class MultiplicityViolationError(GraphQLError):
    """Raised when a multiplicity=one query returns 2+ rows."""

    def __init__(self, operation: str, row_count: int, args: dict[str, Any]) -> None:
        super().__init__(
            f"multiplicity.violation: {operation} returned {row_count} rows "
            "(expected 0 or 1)",
            extensions={
                "code": "multiplicity.violation",
                "operation": operation,
                "row_count": row_count,
            },
        )
