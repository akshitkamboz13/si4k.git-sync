-- si4k.git-sync SQLite Schema
-- Fault-tolerant, lease-locked Tri-Node Git Synchronization Engine

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- 1. Sync Queue (Independent job state machine with distributed worker leases)
CREATE TABLE IF NOT EXISTS sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT UNIQUE NOT NULL,         -- UUIDv4 or deterministic hash
    project_name TEXT NOT NULL,                -- e.g. "PC-Papa", "si4k.search"
    source_node TEXT NOT NULL,                 -- "laptop_gitlab", "server_gitlab", "github"
    target_node TEXT NOT NULL,                 -- "server_gitlab", "github", "laptop_gitlab"
    branch_name TEXT NOT NULL DEFAULT 'main',
    target_ref TEXT,                           -- Optional specific ref (e.g. "refs/heads/main")
    commit_sha TEXT NOT NULL,                  -- Commit hash to synchronize
    status TEXT CHECK(status IN ('queued', 'locked', 'in_progress', 'synced', 'failed', 'dead_letter')) DEFAULT 'queued',
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 5,
    backoff_base_sec INTEGER DEFAULT 30,
    next_retry_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    locked_by TEXT,                            -- Identifier of worker holding lease
    locked_until DATETIME,                     -- Expiration timestamp for lease
    last_error TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 2. GitHub PR Mappings (For Phase 4 PR Bridge)
CREATE TABLE IF NOT EXISTS pr_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    github_repo TEXT NOT NULL,                 -- e.g. "akshitkamboz13/PC-Papa"
    github_pr_number INTEGER NOT NULL,
    gitlab_project_id INTEGER,
    gitlab_mr_iid INTEGER,
    base_branch TEXT NOT NULL DEFAULT 'main',
    base_sha TEXT,                             -- Commit SHA of base when fetched
    head_sha TEXT NOT NULL,                    -- Exact commit SHA of contributor's head
    merge_sha TEXT,                            -- SHA produced after successful merge
    contributor_username TEXT NOT NULL,
    status TEXT CHECK(status IN ('pending_fetch', 'mr_created', 'mr_approved', 'merging', 'merged', 'rejected', 'failed')) DEFAULT 'pending_fetch',
    idempotency_key TEXT UNIQUE NOT NULL,      -- hash(github_repo, pr_number, head_sha)
    last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_name, github_pr_number)
);

-- 3. Node Health Telemetry
CREATE TABLE IF NOT EXISTS node_health (
    node_name TEXT PRIMARY KEY,                -- "laptop_gitlab", "server_gitlab", "github"
    endpoint_url TEXT NOT NULL,
    ssh_host TEXT NOT NULL,
    is_reachable INTEGER CHECK(is_reachable IN (0, 1)) DEFAULT 1,
    last_ping_latency_ms REAL,
    last_error TEXT,
    last_checked_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 4. Audit Log
CREATE TABLE IF NOT EXISTS sync_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT,
    event_type TEXT NOT NULL,                  -- "ENQUEUED", "LEASE_ACQUIRED", "PUSH_SUCCESS", "RETRY_SCHEDULED", "DEAD_LETTER"
    details TEXT,                              -- JSON formatted payload
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Optimized Indexes
CREATE INDEX IF NOT EXISTS idx_sync_queue_lookup ON sync_queue(status, next_retry_at, locked_until);
CREATE INDEX IF NOT EXISTS idx_sync_queue_proj_node ON sync_queue(project_name, target_node, status);
CREATE INDEX IF NOT EXISTS idx_pr_mappings_lookup ON pr_mappings(github_repo, status);
CREATE INDEX IF NOT EXISTS idx_audit_op ON sync_audit_log(operation_id);
