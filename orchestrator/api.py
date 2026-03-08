"""
API Orchestrator
================

Central pipeline that connects all modules and exposes API endpoints.

Endpoints
---------
POST /analyze-repo           — download repo → run analyzer → return analysis
POST /architecture-options   — accept analysis → return ranked architectures
POST /deploy                 — repo URL + chosen architecture → full deploy
GET  /deployment-status/<id> — return deployment status / logs
GET  /deployments            — list all deployments

The module provides two entry-points:
  1. ``app(event, context)`` — AWS Lambda handler (API Gateway proxy)
  2. ``run_local(port)``     — lightweight local HTTP server for development

Pipeline flow:
  Repo URL → repo_analyzer → architecture_engine → (user choice) → deploy_engine
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import urlparse, parse_qs

# Ensure project root is importable
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from repo_analyzer.analyzer import analyze_repo                # noqa: E402
from architecture_engine.engine import recommend_architecture   # noqa: E402
from infra_generator.generator import generate_template, list_targets  # noqa: E402
from deploy_engine.engine import DeployEngine                   # noqa: E402
from orchestrator import persistence                            # noqa: E402

# Re-use download helpers from existing handler
from deploy_handler import (                                    # noqa: E402
    _download,
    _extract_zip,
    _github_zip_url,
    _is_github_http_url,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_deploy_engine = DeployEngine()
persistence.init_db()


def _json_response(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _parse_body(event: dict) -> dict:
    body = event.get("body", "{}")
    if isinstance(body, str):
        return json.loads(body or "{}")
    return body or {}


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_analyze(body: dict) -> dict:
    """POST /analyze-repo"""
    repo_url = body.get("repo_url", "").strip()
    branch = body.get("branch", "main").strip()

    if not repo_url:
        return _json_response(400, {"error": "repo_url is required"})
    if not _is_github_http_url(repo_url):
        return _json_response(400, {"error": "Only https://github.com/<owner>/<repo> URLs are supported"})

    with tempfile.TemporaryDirectory() as td:
        zip_path = os.path.join(td, "repo.zip")
        src_dir = os.path.join(td, "src")
        os.makedirs(src_dir, exist_ok=True)
        try:
            _download(_github_zip_url(repo_url, branch), zip_path)
        except Exception:
            if branch == "main":
                branch = "master"
                _download(_github_zip_url(repo_url, branch), zip_path)
            else:
                raise
        repo_root = _extract_zip(zip_path, src_dir)
        analysis = analyze_repo(repo_root)

    analysis["repo_url"] = repo_url
    analysis["branch"] = branch
    return _json_response(200, analysis)


def _handle_architecture(body: dict) -> dict:
    """POST /architecture-options"""
    analysis = body.get("analysis")
    if not analysis:
        return _json_response(400, {"error": "analysis object is required (output of /analyze-repo)"})

    result = recommend_architecture(analysis)
    return _json_response(200, result)


def _handle_deploy(body: dict) -> dict:
    """POST /deploy"""
    repo_url = body.get("repo_url", "").strip()
    branch = body.get("branch", "main").strip()
    target = body.get("target", "lambda").strip()
    context = body.get("context", {})
    aws_credentials = body.get("aws_credentials")

    if not aws_credentials or not aws_credentials.get("access_key") or not aws_credentials.get("secret_key") or not aws_credentials.get("region"):
        return _json_response(400, {"error": "AWS credentials are required to deploy."})

    if not repo_url:
        return _json_response(400, {"error": "repo_url is required"})

    result = _deploy_engine.start_deployment(
        repo_url=repo_url,
        branch=branch,
        target=target,
        extra_context=context,
        aws_credentials=aws_credentials,
    )
    return _json_response(200, result)


def _handle_status(deploy_id: str) -> dict:
    """GET /deployment-status/<id>"""
    dep = _deploy_engine.get_deployment(deploy_id)
    if not dep:
        return _json_response(404, {"error": f"Deployment {deploy_id} not found"})
    return _json_response(200, dep)


def _handle_list_deployments() -> dict:
    """GET /deployments"""
    return _json_response(200, _deploy_engine.list_deployments())


def _handle_generate_template(body: dict) -> dict:
    """POST /generate-template"""
    target = body.get("target", "")
    context = body.get("context", {})
    if not target:
        return _json_response(400, {"error": "target is required", "available": list_targets()})
    result = generate_template(target, context)
    # Don't include file path in response
    return _json_response(200, {"target": result["target"], "template": result["template"]})


def _handle_full_pipeline(body: dict) -> dict:
    """POST /full-pipeline — convenience endpoint: analyze → recommend → deploy."""
    repo_url = body.get("repo_url", "").strip()
    branch = body.get("branch", "main").strip()
    target_override = body.get("target")  # optional — let user skip recommendation

    aws_credentials = body.get("aws_credentials")
    if not aws_credentials or not aws_credentials.get("access_key") or not aws_credentials.get("secret_key") or not aws_credentials.get("region"):
        return _json_response(400, {"error": "AWS credentials are required to deploy."})

    if not repo_url:
        return _json_response(400, {"error": "repo_url is required"})
    if not _is_github_http_url(repo_url):
        return _json_response(400, {"error": "Only https://github.com/<owner>/<repo> URLs are supported"})

    # Step 1: Analyze
    with tempfile.TemporaryDirectory() as td:
        zip_path = os.path.join(td, "repo.zip")
        src_dir = os.path.join(td, "src")
        os.makedirs(src_dir, exist_ok=True)
        _download(_github_zip_url(repo_url, branch), zip_path)
        repo_root = _extract_zip(zip_path, src_dir)
        analysis = analyze_repo(repo_root)

    # Step 2: Recommend
    recommendation = recommend_architecture(analysis)
    target = target_override or recommendation["recommended"]

    # Step 3: Deploy
    deploy_result = _deploy_engine.start_deployment(
        repo_url=repo_url, branch=branch, target=target,
        aws_credentials=body.get("aws_credentials"),
    )

    return _json_response(200, {
        "analysis": analysis,
        "recommendation": recommendation,
        "deployment": deploy_result,
    })


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def _route(method: str, path: str, body: dict) -> dict:
    """Simple path-based router."""
    path = path.rstrip("/")

    if method == "OPTIONS":
        return _json_response(200, {"status": "ok"})

    if method == "POST":
        if path == "/analyze-repo":
            return _handle_analyze(body)
        if path == "/architecture-options":
            return _handle_architecture(body)
        if path == "/deploy":
            return _handle_deploy(body)
        if path == "/generate-template":
            return _handle_generate_template(body)
        if path == "/full-pipeline":
            return _handle_full_pipeline(body)

    if method == "GET":
        if path == "/deployments":
            return _handle_list_deployments()
        if path.startswith("/deployment-status/"):
            deploy_id = path.split("/")[-1]
            return _handle_status(deploy_id)
        if path == "/health":
            return _json_response(200, {"status": "healthy", "targets": list_targets()})

    return _json_response(404, {"error": f"Unknown route: {method} {path}"})


# ---------------------------------------------------------------------------
# AWS Lambda entry point
# ---------------------------------------------------------------------------

def app(event: dict, context: Any = None) -> dict:
    """AWS Lambda handler compatible with API Gateway proxy integration."""
    try:
        method = event.get("httpMethod", "GET")
        path = event.get("path", "/")
        body = _parse_body(event)
        return _route(method, path, body)
    except Exception as exc:
        return _json_response(500, {"error": str(exc), "trace": traceback.format_exc()})


# ---------------------------------------------------------------------------
# Local HTTP server for development
# ---------------------------------------------------------------------------

_UI_DIR = os.path.join(_PROJECT_ROOT, "ui")

_MIME_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
}


class _LocalHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves UI static files AND delegates API routes."""

    def _send(self, resp: dict) -> None:
        status = resp.get("statusCode", 200)
        self.send_response(status)
        for k, v in resp.get("headers", {}).items():
            self.send_header(k, v)
        self.end_headers()
        body = resp.get("body", "")
        if isinstance(body, bytes):
            self.wfile.write(body)
        else:
            self.wfile.write(body.encode("utf-8"))

    def _serve_static(self, file_path: str) -> bool:
        """Try to serve a static file from the ui/ directory. Returns True if served."""
        if not os.path.isfile(file_path):
            return False
        # Prevent directory traversal
        real = os.path.realpath(file_path)
        if not real.startswith(os.path.realpath(_UI_DIR)):
            return False

        ext = os.path.splitext(file_path)[1].lower()
        mime = _MIME_TYPES.get(ext, "application/octet-stream")

        try:
            with open(file_path, "rb") as f:
                content = f.read()
        except Exception:
            return False

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)
        return True

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]  # strip query string

        # Serve UI dashboard at root
        if path == "/" or path == "":
            if self._serve_static(os.path.join(_UI_DIR, "index.html")):
                return

        # Serve other static files (style.css, app.js, etc.)
        if path.startswith("/"):
            static_path = os.path.join(_UI_DIR, path.lstrip("/"))
            if self._serve_static(static_path):
                return

        # Fall through to API routes
        resp = _route("GET", self.path, {})
        self._send(resp)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        resp = _route("POST", self.path, body)
        self._send(resp)

    def do_OPTIONS(self) -> None:  # noqa: N802
        resp = _route("OPTIONS", self.path, {})
        self._send(resp)

    def log_message(self, fmt, *args) -> None:  # noqa: N802
        print(f"[API] {fmt % args}")


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Multi-threaded HTTP server to handle concurrent requests."""
    daemon_threads = True


def run_local(port: int = 8000) -> None:
    """Start a local dev server on *port*."""
    server = _ThreadingHTTPServer(("", port), _LocalHandler)
    print(f"🚀 Universal Deployer running at http://localhost:{port}")
    print(f"   Dashboard URL: http://localhost:{port}/")
    print(f"   Serving UI from: {os.path.abspath(_UI_DIR)}")
    print(f"   Health check: http://localhost:{port}/health")
    print("   Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Shutting down.")
        server.server_close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Universal Deployer API Server")
    # Priority: Env Var > CLI Arg > Default (8000)
    default_port = int(os.environ.get("PORT", 8000))
    parser.add_argument("--port", type=int, default=default_port, help="Port (default: 8000)")
    args = parser.parse_args()
    run_local(args.port)
