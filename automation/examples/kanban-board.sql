-- Synthetic Hermes Kanban board for the RA-CP-002 projection tests and benchmark.
-- Only the columns the projection reads, with the types of hermes_cli/kanban_db.py's
-- SCHEMA_SQL. No content comes from a real board.
CREATE TABLE tasks (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    status     TEXT NOT NULL,
    priority   INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL,
    child_id  TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);

INSERT INTO tasks VALUES
    ('t_000000a1', 'Add the parser',            'done',     0, 100),
    ('t_000000a2', 'Wire the parser into CLI',  'ready',    0, 200),
    ('t_000000a3', 'Speed up the hot loop',     'ready',    1, 300),
    ('t_000000a4', 'Document the CLI',          'todo',     0, 400),
    ('t_000000a5', 'Retire the old flag',       'archived', 0, 500),
    ('t_000000a6', 'Unapproved spike',          'ready',    5, 600),
    ('t_000000a7', 'Release notes',             'ready',    0, 700),
    ('t_000000a8', 'Wait for the vendor fix',   'scheduled',0, 800);

INSERT INTO task_links VALUES
    ('t_000000a1', 't_000000a2'),
    ('t_000000a2', 't_000000a4'),
    ('t_000000a5', 't_000000a2'),
    ('t_000000a6', 't_000000a7');

INSERT INTO task_events (task_id, kind, payload, created_at) VALUES
    ('t_000000a1', 'created', '{"parents": [], "creator_task_id": null}', 100),
    ('t_000000a2', 'created', '{"parents": ["t_000000a1"], "creator_task_id": "t_000000a1"}', 200),
    ('t_000000a3', 'created', '{"parents": [], "creator_task_id": "t_000000a1"}', 300),
    ('t_000000a4', 'created', '{"parents": ["t_000000a2"]}', 400),
    ('t_000000a5', 'created', '{}', 500),
    ('t_000000a6', 'created', '{}', 600),
    ('t_000000a7', 'created', '{"parents": ["t_000000a6"]}', 700),
    ('t_000000a8', 'created', 'not json', 800),
    ('t_000000a1', 'completed', '{}', 150);
