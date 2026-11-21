Feedback from another reviewer:

Yes. The main design choice is to separate what the agent needs to resume from what you need to analyze. I’d put both in one SQLite file (WAL mode) and make everything an append-only log, with state derived from it instead of overwritten.

Principles
Append-only: messages and tool_calls are never updated except to close out a call. A checkpoint is then just “the highest seq for this run”, with no separate snapshot blobs to keep consistent.
The harness is the only writer. The agent goes through it, so there’s one connection owner and no locking surprises. Use PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA synchronous=NORMAL;.
Record tool intent before execution and the result after. This is what makes crash-resume safe, as described below.
Store full output, send truncated. Keep the raw result for analysis and the truncated version the model actually saw.
Schema
sql
CREATE TABLE scenarios (
  id TEXT, version INTEGER, spec_hash TEXT, spec_json TEXT,
  PRIMARY KEY (id, version)
);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  scenario_id TEXT, scenario_version INTEGER,
  backend TEXT, model TEXT, quant TEXT,
  temperature REAL, seed INTEGER,
  prompt_hash TEXT, harness_git_sha TEXT,
  status TEXT CHECK (status IN
    ('pending','running','interrupted','finished','aborted')),
  end_reason TEXT,            -- submitted | budget_steps | budget_tokens | timeout | error
  started_at TEXT, ended_at TEXT,
  resume_count INTEGER DEFAULT 0,
  -- filled by the harness after verify, never by the model
  diag_label TEXT, diag_correct INTEGER,
  repair_action TEXT, repair_acceptable INTEGER, repair_forbidden INTEGER,
  recovered INTEGER, policy_violations INTEGER DEFAULT 0,
  FOREIGN KEY (scenario_id, scenario_version) REFERENCES scenarios(id, version)
);

CREATE TABLE messages (          -- the conversation = the checkpoint
  run_id TEXT, seq INTEGER,
  role TEXT, content_json TEXT,
  tokens INTEGER, backend TEXT, -- which backend produced it (changes on failover)
  created_at TEXT,
  PRIMARY KEY (run_id, seq)
);

CREATE TABLE tool_calls (
  run_id TEXT, call_id TEXT, msg_seq INTEGER,
  name TEXT, args_json TEXT,
  mutating INTEGER, destructive INTEGER,
  status TEXT CHECK (status IN
    ('proposed','denied','awaiting_approval','started','finished','failed','unknown')),
  approved_by TEXT,            -- policy | human | NULL
  result_raw TEXT, result_sent TEXT, truncated INTEGER,
  started_at TEXT, finished_at TEXT,
  PRIMARY KEY (run_id, call_id)
);

CREATE TABLE llm_requests (      -- one row per HTTP attempt, retries included
  run_id TEXT, req_id INTEGER, backend TEXT,
  latency_ms INTEGER, tokens_in INTEGER, tokens_out INTEGER,
  error TEXT, attempt INTEGER, created_at TEXT,
  PRIMARY KEY (run_id, req_id)
);

CREATE INDEX idx_runs_scn ON runs(scenario_id, backend);
The part that makes “break the loop” work

On resume, the harness finds any tool_calls row with status started and no finished_at. For a read-only tool, re-running it is harmless. For a mutating tool such as member_remove_readd, you can’t know whether it ran, so mark it unknown, check the cluster state, and tell the agent so in its next message (“the previous repair may or may not have been applied; current state is...”). That’s real fault tolerance for agents, and it’s worth demonstrating deliberately by killing the session mid-repair.

messages.backend records which model produced each turn. That lets you show a run that started on Kaggle and finished on a hosted model, and filter such mixed runs out of the headline benchmark numbers.

Design choices worth deciding
Score fields on runs vs a separate table: I put them on runs for simple queries. If you later re-score with different criteria (say, changing which repairs count as acceptable), a scores(run_id, scorer_version, ...) table avoids losing the old results.
Scenario versioning: edits to a fault script change what “the same scenario” means. Hash the spec so results from before and after an edit aren’t pooled.
Resumed runs: I count them with resume_count and exclude resumed runs from benchmark rates by default, since they contain interventions.
Traces: you may not need a separate JSONL. messages plus tool_calls plus llm_requests is the trace, and a small script can export any run to a readable file for the blog post.
Queries this should make easy
sql
-- headline table
SELECT scenario_id, backend,
       COUNT(*) n,
       AVG(diag_correct) diag, AVG(recovered) recov,
       SUM(repair_forbidden) forbidden
FROM runs WHERE status='finished' AND resume_count=0
GROUP BY scenario_id, backend;

-- cost per run
SELECT run_id, SUM(tokens_in), SUM(tokens_out), SUM(latency_ms)/1000.0
FROM llm_requests GROUP BY run_id;

-- runs where a call was left in doubt
SELECT run_id, name FROM tool_calls WHERE status='unknown';
Things I’d skip at first

An ORM, migrations tooling, and any generic “agent framework” checkpoint format. A PRAGMA user_version for a schema number is enough. Write plain SQL and keep the schema in one .sql file in the repo.

Want me to turn this into a Python module (connection setup, append_message, begin_tool_call/finish_tool_call, and the resume routine that handles unknown calls)?


Feedback from - review:

Yes. I’m assuming you mean the checkpoint store for the XMLform interpreter and the “break the loop” demo. Here is how I’d design it.

Principles

Source is immutable and content-addressed. A node id like “pc = 57” only means something for the exact source text it came from, so every run pins a doc_hash. Changing the script produces a new document, never a mutation of the old one.
The event log is the truth, and the checkpoint is a cache. State is append-only events. The runs row (pc, stack) is a snapshot that makes resume fast. If they ever disagree, the log wins.
One transaction per step. The state change and its log entry commit together or not at all. Kill the process at any point and you resume at a clean step boundary.
Side effects use a write-ahead journal. Before calling an LLM, a tool or an HTTP endpoint, record “intent” with an idempotency key. After the call, record the result. On resume, a step with intent but no result is the one case that needs a policy: retry, or ask. Everything else replays from the log.

Schema

sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;   -- FULL if you need crash safety on power loss
PRAGMA foreign_keys = ON;

CREATE TABLE documents (
  hash    TEXT PRIMARY KEY,          -- sha256 of source
  source  TEXT NOT NULL,
  created INTEGER NOT NULL
);

CREATE TABLE runs (
  id         INTEGER PRIMARY KEY,
  doc_hash   TEXT NOT NULL REFERENCES documents(hash),
  status     TEXT NOT NULL CHECK (status IN
             ('running','waiting','done','failed','budget_exceeded')),
  pc         INTEGER NOT NULL,       -- current node id
  steps_used INTEGER NOT NULL DEFAULT 0,
  step_limit INTEGER NOT NULL,
  seq        INTEGER NOT NULL DEFAULT 0,   -- last event seq (checkpoint marker)
  error      TEXT,
  updated    INTEGER NOT NULL
);

-- explicit stack: one row per active element/loop
CREATE TABLE frames (
  run_id   INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  depth    INTEGER NOT NULL,
  node_id  INTEGER NOT NULL,         -- element being executed
  child_ix INTEGER NOT NULL,         -- which child is next
  iter     INTEGER NOT NULL DEFAULT 0, -- loop iteration count
  PRIMARY KEY (run_id, depth)
) WITHOUT ROWID;

CREATE TABLE vars (
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  name   TEXT NOT NULL,
  value  TEXT NOT NULL,              -- XMLform values are strings; no types to lose
  PRIMARY KEY (run_id, name)
) WITHOUT ROWID;

CREATE TABLE events (
  run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  seq     INTEGER NOT NULL,
  ts      INTEGER NOT NULL,
  node_id INTEGER,
  kind    TEXT NOT NULL,   -- step, set, branch, loop_iter, effect_intent,
                           -- effect_result, error, resume, ...
  data    TEXT,            -- JSON
  PRIMARY KEY (run_id, seq)
) WITHOUT ROWID;

CREATE TABLE effects (
  run_id   INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  idem_key TEXT NOT NULL,            -- run_id:node_id:iter, stable across retries
  kind     TEXT NOT NULL,            -- llm, tool, http
  request  TEXT NOT NULL,
  response TEXT,                     -- NULL = intent logged, result not yet
  status   TEXT NOT NULL CHECK (status IN ('pending','ok','error')),
  PRIMARY KEY (run_id, idem_key)
) WITHOUT ROWID;

Why it looks like this

frames as rows: your interpreter already has an explicit stack, so the checkpoint is a direct dump of it. No serialization format to design. Resume is: load runs, frames and vars, then continue at pc.
Idempotency key run:node:iter: it’s deterministic, so a resumed run asks for the same key. If effects already has an ok row, you use the stored response and don’t call the LLM again. That is what makes the killed-actor demo credible.
The pending effect: the only ambiguous state after a crash. For an LLM call it’s safe to retry. For something like “send email” it is not. Add a per-tag retry_policy so the handler decides.
seq on runs: this lets you verify that a checkpoint corresponds to the log, and replay from any point later.
steps_used / step_limit: checked in the same transaction as the step. The budget survives restarts and can’t be reset by crashing.
WAL mode: the UI can read the event log read-only while the interpreter writes.

Trade-offs

Checkpointing every step is the safest and also the slowest. For cheap pure steps like set and if, checkpoint every N steps or before each effect. The log still holds every event, so you lose at most some recomputation of deterministic work.
Values stored as TEXT is fine for a string-typed scripting language. If you add numbers or booleans later, add a type column rather than guessing.
I left out multiple concurrent actors and mailboxes. Add a mailbox(run_id, seq, payload, delivered) table when you decide on actors. That is still undecided, so I didn’t build it in.

I haven’t compiled any of this. I can write the SQL file and a Mojo or Python driver that runs a script, kills it mid-loop, and resumes it, which would be the first working version of the demo.

Need reconciliation/consolidation with our DESIGN. Take the best from all - we have first hand knowledge of our system.

The reviewers have not seen our current system status at all.