"""
Main CLI entrypoint for si4k.git-sync.
Usage:
    si4k-sync init
    si4k-sync status
    si4k-sync health
    si4k-sync enqueue --project <name> --source <node> --target <node> --branch <branch> --sha <sha>
    si4k-sync worker [--once]
"""

import argparse
import sys
from pathlib import Path

from core.config import SyncConfig
from core.models import NodeType, SyncStatus
from core.queue_leaser import QueueLeaser
from database.db import get_connection, init_db
from engine.health_checker import HealthChecker
from engine.worker import SyncWorker


def cmd_init(args, config: SyncConfig):
    print(f"🔧 Initializing database at: {config.db_path}...")
    db_file = init_db(config.db_path)
    print(f"✅ SQLite schema successfully created and configured (WAL mode): {db_file}")


def cmd_health(args, config: SyncConfig):
    init_db(config.db_path)
    checker = HealthChecker(config)
    print("🩺 Probing node health across Tri-Node Ecosystem...\n")
    results = checker.probe_all()

    print(f"{'Node':<16} | {'Status':<12} | {'Latency':<10} | {'Endpoint'}")
    print("-" * 65)
    for node, data in results.items():
        status_badge = "🟢 ONLINE" if data["is_reachable"] else "🔴 OFFLINE"
        latency_str = f"{data['latency_ms']:.1f}ms" if data["is_reachable"] else "N/A"
        print(f"{node:<16} | {status_badge:<12} | {latency_str:<10} | {data['endpoint_url']}")
        if not data["is_reachable"] and data["error"]:
            print(f"   ↳ Error: {data['error']}")
    print("")


def cmd_status(args, config: SyncConfig):
    init_db(config.db_path)
    leaser = QueueLeaser(config.db_path)
    stats = leaser.queue_stats()

    print("📊 si4k.git-sync System Status")
    print("=" * 60)
    print(f"Database: {config.db_path}")
    print(f"Worker ID: {config.worker_id}")
    print(f"Queue Stats: Total: {stats.get('total', 0)} | Queued: {stats.get('queued', 0)} | "
          f"Locked/In-Progress: {stats.get('locked', 0) + stats.get('in_progress', 0)} | "
          f"Synced: {stats.get('synced', 0)} | Failed: {stats.get('failed', 0)} | Dead-Letter: {stats.get('dead_letter', 0)}")
    print("-" * 60)

    # Node Health
    with get_connection(config.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM node_health ORDER BY node_name;")
        nodes = cursor.fetchall()

    if nodes:
        print("\n🌐 Node Health:")
        print(f"{'Node':<16} | {'Status':<10} | {'Latency':<10} | {'Last Checked'}")
        print("-" * 55)
        for n in nodes:
            badge = "🟢 OK" if n["is_reachable"] else "🔴 DOWN"
            lat = f"{n['last_ping_latency_ms']:.1f}ms" if n["last_ping_latency_ms"] else "N/A"
            print(f"{n['node_name']:<16} | {badge:<10} | {lat:<10} | {n['last_checked_at']}")
    else:
        print("\n🌐 Node Health: No health telemetry recorded yet (run 'si4k-sync health').")

    # Recent Queue Entries
    jobs = leaser.list_jobs(limit=10)
    if jobs:
        print("\n📋 Recent Jobs (Last 10):")
        print(f"{'ID':<4} | {'Project':<15} | {'Source -> Target':<25} | {'Branch':<8} | {'Status':<11} | {'Retries'}")
        print("-" * 75)
        for j in jobs:
            route = f"{j.source_node[:6]} -> {j.target_node[:6]}"
            print(f"{j.id:<4} | {j.project_name:<15} | {route:<25} | {j.branch_name:<8} | {j.status.value:<11} | {j.retry_count}/{j.max_retries}")
            if j.last_error:
                print(f"     ↳ Error: {j.last_error[:70]}")
    else:
        print("\n📋 Queue is empty.")
    print("")


def cmd_enqueue(args, config: SyncConfig):
    init_db(config.db_path)
    leaser = QueueLeaser(config.db_path)
    job = leaser.enqueue(
        project_name=args.project,
        source_node=args.source,
        target_node=args.target,
        branch_name=args.branch,
        commit_sha=args.sha,
        target_ref=args.target_ref,
        operation_id=args.op_id,
    )
    print(f"✅ Job enqueued successfully: #{job.id} [{job.operation_id}]")
    print(f"   Route: {job.project_name} ({job.source_node} -> {job.target_node}) @ {job.commit_sha[:8]}")


def cmd_worker(args, config: SyncConfig):
    init_db(config.db_path)
    worker = SyncWorker(config)
    worker.start(run_once=args.once)


def cmd_pr_list(args, config: SyncConfig):
    init_db(config.db_path)
    with get_connection(config.db_path) as conn:
        cursor = conn.cursor()
        if args.project:
            cursor.execute("SELECT * FROM pr_mappings WHERE project_name = ? ORDER BY id DESC;", (args.project,))
        else:
            cursor.execute("SELECT * FROM pr_mappings ORDER BY id DESC;")
        rows = cursor.fetchall()

    print(f"\n🔀 Tracked Pull Requests ({len(rows)} total)")
    print("=" * 80)
    if not rows:
        print("No pull requests tracked yet. Run 'si4k-sync pr-ingest --project <name>' to check.\n")
        return

    print(f"{'PR #':<6} | {'Project':<16} | {'Contributor':<15} | {'Status':<12} | {'GitLab MR':<10} | {'HEAD SHA'}")
    print("-" * 80)
    for r in rows:
        mr_str = f"!{r['gitlab_mr_iid']}" if r['gitlab_mr_iid'] else "N/A"
        print(f"#{r['github_pr_number']:<5} | {r['project_name']:<16} | @{r['contributor_username']:<14} | {r['status']:<12} | {mr_str:<10} | {r['head_sha'][:8]}")
        if r['merge_sha']:
            print(f"       ↳ Merged Commit SHA: {r['merge_sha'][:8]}")
    print("")


def cmd_pr_ingest(args, config: SyncConfig):
    init_db(config.db_path)
    from bridge.pr_ingestor import PRIngestor
    ingestor = PRIngestor(config)
    print(f"🔍 Ingesting external PRs for repository: {args.project}...")
    results = ingestor.ingest_repo_prs(args.project)
    if not results:
        print("ℹ️ No new pull requests to ingest.\n")
    else:
        print(f"\n✅ Ingested {len(results)} pull request(s):")
        for res in results:
            print(f"   • PR #{res['pr_number']} by @{res['contributor']} ➔ GitLab MR !{res['mr_iid']} (Branch: github-pr/{res['pr_number']}-{res['contributor']})")
        print("")


def cmd_pr_merge(args, config: SyncConfig):
    init_db(config.db_path)
    from bridge.merge_executor import HybridMergeExecutor
    executor = HybridMergeExecutor(config)
    print(f"🚀 Executing 6-Step Hybrid Merge Protocol for {args.project} PR #{args.pr}...")
    res = executor.execute_merge(args.project, args.pr, custom_commit_title=args.title)
    print(f"\n🎉 Success! PR #{args.pr} merged natively on GitHub!")
    print(f"   • Merge Commit SHA: {res['merge_sha']}")
    print(f"   • Author attribution and GitHub contribution graph preserved.")
    print(f"   • Replicated to Home Server GitLab and enqueued for Laptop GitLab.\n")


def cmd_issue_list(args, config: SyncConfig):
    init_db(config.db_path)
    with get_connection(config.db_path) as conn:
        cursor = conn.cursor()
        if args.project:
            cursor.execute("SELECT * FROM issue_mappings WHERE project_name = ? ORDER BY id DESC;", (args.project,))
        else:
            cursor.execute("SELECT * FROM issue_mappings ORDER BY id DESC;")
        rows = cursor.fetchall()

    print(f"\n📋 Tracked Issues ({len(rows)} total)")
    print("=" * 85)
    if not rows:
        print("No issues tracked yet. Run 'si4k-sync issue-sync --project <name>' to synchronize.\n")
        return

    print(f"{'Issue #':<8} | {'Project':<16} | {'Author':<15} | {'State':<8} | {'GitLab #':<10} | {'Title'}")
    print("-" * 85)
    for r in rows:
        gl_str = f"#{r['gitlab_issue_iid']}" if r['gitlab_issue_iid'] else "N/A"
        badge = "🟢 OPEN" if r['state'] == 'open' else "🔒 CLOSED"
        print(f"GH #{r['github_issue_number']:<5} | {r['project_name']:<16} | @{r['author_username']:<14} | {badge:<8} | {gl_str:<10} | {r['title'][:25]}")
    print("")


def cmd_issue_sync(args, config: SyncConfig):
    init_db(config.db_path)
    from bridge.issue_bridge import TwoWayIssueBridge
    bridge = TwoWayIssueBridge(config)
    print(f"🔄 Running Two-Way Issue Sync for {args.project}...")
    stats = bridge.sync_project_issues(args.project)
    print(f"\n✅ Issue Synchronization Summary:")
    print(f"   • Inbound Issues Synced (GitHub -> GitLab): {stats['github_to_gitlab_created']}")
    print(f"   • GitHub Closures Synced to GitLab:        {stats['github_to_gitlab_closed']}")
    print(f"   • GitLab Closures Synced to GitHub:        {stats['gitlab_to_github_closed']}")
    print(f"   • Comments Mirrored Across Threads:        {stats['comments_synced']}\n")


def main():
    parser = argparse.ArgumentParser(
        prog="si4k-sync",
        description="si4k.git-sync: Fault-Tolerant Tri-Node Git Replication Engine",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # init
    p_init = subparsers.add_parser("init", help="Initialize the SQLite database schema")

    # status
    p_status = subparsers.add_parser("status", help="Inspect queue statistics and node health")

    # health
    p_health = subparsers.add_parser("health", help="Probe reachability of all 3 Git nodes")

    # enqueue
    p_enq = subparsers.add_parser("enqueue", help="Enqueue a new replication task")
    p_enq.add_argument("--project", "-p", required=True, help="Project name (e.g. PC-Papa)")
    p_enq.add_argument("--source", "-s", required=True, choices=["laptop_gitlab", "server_gitlab", "github"])
    p_enq.add_argument("--target", "-t", required=True, choices=["laptop_gitlab", "server_gitlab", "github"])
    p_enq.add_argument("--branch", "-b", default="main", help="Branch name (default: main)")
    p_enq.add_argument("--sha", required=True, help="Commit SHA to replicate")
    p_enq.add_argument("--target-ref", help="Optional target refspec")
    p_enq.add_argument("--op-id", help="Optional deterministic operation ID")

    # worker
    p_worker = subparsers.add_parser("worker", help="Start the background sync worker daemon")
    p_worker.add_argument("--once", action="store_true", help="Process one job then exit")

    # pr-list
    p_pr_list = subparsers.add_parser("pr-list", help="List tracked GitHub pull requests")
    p_pr_list.add_argument("--project", "-p", help="Filter by project name")

    # pr-ingest
    p_pr_ingest = subparsers.add_parser("pr-ingest", help="Ingest open PRs from GitHub into GitLab MRs")
    p_pr_ingest.add_argument("--project", "-p", required=True, help="Project name (e.g. PC-Papa)")

    # pr-merge
    p_pr_merge = subparsers.add_parser("pr-merge", help="Execute 6-step race-safe hybrid merge for an approved PR")
    p_pr_merge.add_argument("--project", "-p", required=True, help="Project name (e.g. PC-Papa)")
    p_pr_merge.add_argument("--pr", type=int, required=True, help="GitHub PR number")
    p_pr_merge.add_argument("--title", help="Optional custom merge commit title")

    # issue-list
    p_iss_list = subparsers.add_parser("issue-list", help="List tracked issues")
    p_iss_list.add_argument("--project", "-p", help="Filter by project name")

    # issue-sync
    p_iss_sync = subparsers.add_parser("issue-sync", help="Execute two-way issue and comment sync")
    p_iss_sync.add_argument("--project", "-p", required=True, help="Project name (e.g. PC-Papa)")

    args = parser.parse_args()
    config = SyncConfig.load()

    handlers = {
        "init": cmd_init,
        "status": cmd_status,
        "health": cmd_health,
        "enqueue": cmd_enqueue,
        "worker": cmd_worker,
        "pr-list": cmd_pr_list,
        "pr-ingest": cmd_pr_ingest,
        "pr-merge": cmd_pr_merge,
        "issue-list": cmd_issue_list,
        "issue-sync": cmd_issue_sync,
    }

    handler = handlers.get(args.subcommand)
    if handler:
        handler(args, config)


if __name__ == "__main__":
    main()


