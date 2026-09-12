-- ProjectTracker's core schema.
--
-- Placeholder slice: `users` only. The rest of the tables this world has
-- (teams, projects, issues, labels, comments, events) arrive with the tools
-- that read them, in their own numbered files beside this one.
--
-- Every table is STRICT with an explicit primary key, and every timestamp is
-- canonical text (`2026-06-01T09:00:00.000Z`) written by the tool that makes
-- the row, never by a DDL default: one format across every door, and no
-- wall-clock expression anywhere in the schema.

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    created_at TEXT NOT NULL
) STRICT;
