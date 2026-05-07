-- Phase 1 deterministic seed: 100 teams, 10000 users round-robin assigned.
-- Re-seeding requires `make down && make up` because /docker-entrypoint-initdb.d/*.sql
-- runs only on first volume init.

CREATE TABLE teams (
    id BIGINT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE users (
    id BIGINT PRIMARY KEY,
    full_name TEXT NOT NULL,
    team_id BIGINT NOT NULL REFERENCES teams(id)
);

INSERT INTO teams (id, name)
SELECT i, 'team-' || i
FROM generate_series(1, 100) AS i;

INSERT INTO users (id, full_name, team_id)
SELECT i, 'user-' || i, ((i - 1) % 100) + 1
FROM generate_series(1, 10000) AS i;
