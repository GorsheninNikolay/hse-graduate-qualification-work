"""Phase 3 Locust load profile (roadmap §4 lines 153-156).

Three User classes covering the experiment matrix's scenario axis:

  - ReadHeavyUser:   80% getUser  / 20% listUsersByTeam
  - WriteMixUser:    60% getUser  / 30% updateUserName / 10% listUsersByTeam
  - MutationBurstUser: 50% updateUserName / 50% getUser

Each task POSTs a parameterized GraphQL query to /graphql with `variables` JSON
- the query string itself is constant (cache-friendly, prepared-statement-like),
all dynamism is in the variables. Random IDs match examples/seed.sql cardinality
(10000 users, 100 teams).

Phase 4's matrix runner narrows to one user class per cell via Locust's
--user-classes flag; P3-07 (loadtest/runner.py) maps scenario name to class.
"""

import random

from locust import HttpUser, constant, task
from locust.clients import HttpSession

# Imported into this module's namespace so locust auto-discovers the LoadTestShape
# subclass when launched via `locust -f loadtest/locustfile.py` (locust 2.43 has
# no --shape-class CLI flag; auto-discovery is the only mechanism).
from loadtest.shape import ExperimentShape  # noqa: F401


GET_USER_QUERY = (
    "query getUser($id: ID!) { getUser(id: $id) { id name teamId } }"
)
LIST_USERS_BY_TEAM_QUERY = (
    "query listUsersByTeam($teamId: ID!) "
    "{ listUsersByTeam(teamId: $teamId) { id name } }"
)
UPDATE_USER_NAME_MUTATION = (
    "mutation updateUserName($id: ID!, $name: String!) "
    "{ updateUserName(id: $id, name: $name) { id name } }"
)


def _post_gql(
    client: HttpSession, query: str, variables: dict[str, object]
) -> None:
    """POST a parameterized GraphQL request and discard the response body.

    `name` parameter on client.post groups statistics by operation name in
    the locust report (so the *_stats.csv aggregator separates per-op
    latencies for the report writer in P3-07).
    """
    op_name = query.split()[1]  # "query getUser ..." -> "getUser"
    client.post(
        "/graphql/",
        json={"query": query, "variables": variables},
        name=f"POST /graphql/ {op_name}",
    )


class ReadHeavyUser(HttpUser):
    """80% getUser, 20% listUsersByTeam — pure read workload."""
    wait_time = constant(0)

    @task(80)
    def get_user(self) -> None:
        user_id = str(random.randint(1, 10000))
        _post_gql(self.client, GET_USER_QUERY, {"id": user_id})

    @task(20)
    def list_users_by_team(self) -> None:
        team_id = str(random.randint(1, 100))
        _post_gql(self.client, LIST_USERS_BY_TEAM_QUERY, {"teamId": team_id})


class WriteMixUser(HttpUser):
    """60% getUser, 30% updateUserName, 10% listUsersByTeam — read-mutation mix."""
    wait_time = constant(0)

    @task(60)
    def get_user(self) -> None:
        user_id = str(random.randint(1, 10000))
        _post_gql(self.client, GET_USER_QUERY, {"id": user_id})

    @task(30)
    def update_user_name(self) -> None:
        user_id = str(random.randint(1, 10000))
        new_name = f"u-{random.randint(0, 1_000_000)}"
        _post_gql(
            self.client,
            UPDATE_USER_NAME_MUTATION,
            {"id": user_id, "name": new_name},
        )

    @task(10)
    def list_users_by_team(self) -> None:
        team_id = str(random.randint(1, 100))
        _post_gql(self.client, LIST_USERS_BY_TEAM_QUERY, {"teamId": team_id})


class MutationBurstUser(HttpUser):
    """50% updateUserName, 50% getUser — invalidation-heavy workload."""
    wait_time = constant(0)

    @task(50)
    def update_user_name(self) -> None:
        user_id = str(random.randint(1, 10000))
        new_name = f"u-{random.randint(0, 1_000_000)}"
        _post_gql(
            self.client,
            UPDATE_USER_NAME_MUTATION,
            {"id": user_id, "name": new_name},
        )

    @task(50)
    def get_user(self) -> None:
        user_id = str(random.randint(1, 10000))
        _post_gql(self.client, GET_USER_QUERY, {"id": user_id})
