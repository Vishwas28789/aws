import sqlite3
import json
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deployments.db")

def init_db():
    """Initialize the SQLite database schema."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Deployments table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS deployments (
            id TEXT PRIMARY KEY,
            repo_url TEXT,
            branch TEXT,
            target TEXT,
            status TEXT,
            url TEXT,
            error TEXT,
            started_at TEXT,
            metadata TEXT
        )
    ''')
    
    # Logs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deploy_id TEXT,
            message TEXT,
            timestamp TEXT,
            FOREIGN KEY (deploy_id) REFERENCES deployments (id)
        )
    ''')
    
    conn.commit()
    conn.close()

def save_deployment(dep: dict):
    """Insert or update a deployment record."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Extract metadata (non-standard fields)
    standard_fields = {"id", "repo_url", "branch", "target", "status", "url", "error", "started_at"}
    metadata = {k: v for k, v in dep.items() if k not in standard_fields and k != "logs"}
    
    cursor.execute('''
        INSERT OR REPLACE INTO deployments 
        (id, repo_url, branch, target, status, url, error, started_at, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        dep.get("id"),
        dep.get("repo_url"),
        dep.get("branch"),
        dep.get("target"),
        dep.get("status"),
        dep.get("url"),
        dep.get("error"),
        dep.get("started_at"),
        json.dumps(metadata)
    ))
    
    conn.commit()
    conn.close()

def append_log(deploy_id: str, message: str):
    """Append a log entry for a specific deployment."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO logs (deploy_id, message, timestamp)
        VALUES (?, ?, ?)
    ''', (
        deploy_id,
        message,
        datetime.now(timezone.utc).isoformat()
    ))
    
    conn.commit()
    conn.close()

def get_deployment(deploy_id: str) -> dict | None:
    """Retrieve a full deployment record including logs."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM deployments WHERE id = ?", (deploy_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return None
    
    dep = dict(row)
    
    # Parse metadata
    if dep.get("metadata"):
        dep.update(json.loads(dep["metadata"]))
        del dep["metadata"]
    
    # Fetch logs
    cursor.execute("SELECT message FROM logs WHERE deploy_id = ? ORDER BY id ASC", (deploy_id,))
    dep["logs"] = [r["message"] for r in cursor.fetchall()]
    
    conn.close()
    return dep

def list_deployments() -> list[dict]:
    """List all deployments (summary only, no logs)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, repo_url, target, status, url, started_at FROM deployments ORDER BY started_at DESC")
    rows = [dict(r) for r in cursor.fetchall()]
    
    conn.close()
    return rows

if __name__ == "__main__":
    # Self-test / initialization
    init_db()
    print(f"Database initialized at {DB_PATH}")
