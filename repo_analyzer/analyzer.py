"""
Repo Analyzer — scans a repository directory and produces a structured analysis.

Detects:
  - Programming language(s)
  - Framework
  - Repo type (static_site, frontend_app, backend_api, fullstack, monorepo)
  - Dockerized flag
  - Deployment target recommendations
  - Repo health signals (README, tests, CI config, .env exposure)
  - Security warnings (exposed secrets patterns)
"""

import json
import os
import re
from typing import Any


# ---------------------------------------------------------------------------
# Marker file → language / framework detection rules
# ---------------------------------------------------------------------------

_LANGUAGE_MARKERS: list[tuple[str, str]] = [
    ("package.json", "node"),
    ("requirements.txt", "python"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("Pipfile", "python"),
    ("pom.xml", "java"),
    ("build.gradle", "java"),
    ("build.gradle.kts", "java"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
    ("Gemfile", "ruby"),
    ("*.csproj", "dotnet"),
    ("*.fsproj", "dotnet"),
    ("*.sln", "dotnet"),
]

_FRAMEWORK_MARKERS: list[tuple[str, str]] = [
    ("next.config.js", "nextjs"),
    ("next.config.mjs", "nextjs"),
    ("next.config.ts", "nextjs"),
    ("nuxt.config.js", "nuxt"),
    ("nuxt.config.ts", "nuxt"),
    ("angular.json", "angular"),
    ("vite.config.js", "vite"),
    ("vite.config.ts", "vite"),
    ("svelte.config.js", "svelte"),
    ("gatsby-config.js", "gatsby"),
    ("remix.config.js", "remix"),
    ("astro.config.mjs", "astro"),
]

_SECRETS_PATTERNS: list[re.Pattern] = [
    re.compile(r"AKIA[0-9A-Z]{16}", re.ASCII),                  # AWS access key
    re.compile(r"(?i)(password|secret|api_key)\s*=\s*['\"].+['\"]"),
    re.compile(r"ghp_[A-Za-z0-9_]{36,}"),                       # GitHub PAT
    re.compile(r"sk-[A-Za-z0-9]{32,}"),                         # OpenAI key
]

_SECRET_SCAN_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".env", ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _exists(root: str, name: str) -> bool:
    """Check if *name* exists inside *root*.  Supports simple globs like ``*.csproj``."""
    if "*" in name:
        import fnmatch
        return any(fnmatch.fnmatch(f, name) for f in os.listdir(root))
    return os.path.exists(os.path.join(root, name))


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _detect_languages(root: str) -> list[str]:
    langs: list[str] = []
    for marker, lang in _LANGUAGE_MARKERS:
        if _exists(root, marker) and lang not in langs:
            langs.append(lang)
    return langs or ["unknown"]


def _detect_framework(root: str, languages: list[str]) -> str | None:
    # Explicit framework marker files
    for marker, fw in _FRAMEWORK_MARKERS:
        if _exists(root, marker):
            return fw

    # Inspect package.json dependencies
    if "node" in languages:
        pkg = _read_json(os.path.join(root, "package.json"))
        deps: dict = {}
        deps.update(pkg.get("dependencies", {}) or {})
        deps.update(pkg.get("devDependencies", {}) or {})
        # Next.js (check dep even if config file missing)
        if "next" in deps:
            return "nextjs"
        if "react-scripts" in deps:
            return "create-react-app"
        if "react" in deps:
            return "react"
        if "vue" in deps:
            return "vue"
        if "@angular/core" in deps:
            return "angular"
        if "express" in deps:
            return "express"
        if "fastify" in deps:
            return "fastify"
        if "hapi" in deps or "@hapi/hapi" in deps:
            return "hapi"
        if "koa" in deps:
            return "koa"
        if "@nestjs/core" in deps or "@nestjs/common" in deps:
            return "nestjs"

    # Python frameworks — check requirements.txt, pyproject.toml, setup.py
    if "python" in languages:
        py_text = ""
        for fname in ("requirements.txt", "pyproject.toml", "setup.py", "Pipfile"):
            fpath = os.path.join(root, fname)
            if os.path.isfile(fpath):
                try:
                    py_text += open(fpath, encoding="utf-8").read().lower() + "\n"
                except Exception:
                    pass
        if _exists(root, "manage.py") or "django" in py_text:
            return "django"
        if "fastapi" in py_text:
            return "fastapi"
        if "flask" in py_text:
            return "flask"
        if "sanic" in py_text:
            return "sanic"

    # Java — Spring Boot
    if "java" in languages:
        pom = os.path.join(root, "pom.xml")
        if os.path.isfile(pom):
            try:
                txt = open(pom, encoding="utf-8").read()
                if "spring-boot" in txt:
                    return "spring-boot"
            except Exception:
                pass

    return None


def _detect_repo_type(root: str, languages: list[str], framework: str | None) -> str:
    _FRONTEND_FRAMEWORKS = {
        "react", "create-react-app", "vue", "angular", "svelte",
        "nuxt", "gatsby", "vite", "astro", "remix",
    }
    _BACKEND_FRAMEWORKS = {
        "express", "fastify", "hapi", "koa", "nestjs",
        "flask", "django", "fastapi", "sanic", "spring-boot",
    }
    has_frontend_marker = framework in _FRONTEND_FRAMEWORKS
    has_backend_marker = framework in _BACKEND_FRAMEWORKS

    # Check for docker
    if _exists(root, "Dockerfile") or _exists(root, "docker-compose.yml") or _exists(root, "docker-compose.yaml"):
        return "docker_app"

    # Next.js is its own category (SSR + static)
    if framework == "nextjs":
        return "nextjs_app"

    if has_frontend_marker and has_backend_marker:
        return "fullstack"
    if has_frontend_marker:
        return "frontend_app"
    if has_backend_marker:
        # Check for Node backend entry points (server.js, app.js, index.js)
        if "node" in languages:
            _NODE_ENTRY_FILES = ("server.js", "app.js", "index.js")
            has_entry = any(_exists(root, f) for f in _NODE_ENTRY_FILES)
            if has_entry:
                return "node_backend"
            return "node_backend"  # Backend framework detected = node_backend
        return "python_api" if "python" in languages else "backend_api"

    # Lambda API — check for handler.js, lambda.js, serverless.yml, or exports.handler
    _LAMBDA_MARKERS = ("handler.js", "lambda.js", "serverless.yml", "serverless.yaml")
    has_lambda_marker = any(_exists(root, m) for m in _LAMBDA_MARKERS)
    if not has_lambda_marker:
        # Check for exports.handler in JS files at root
        for js_name in ("index.js", "handler.js", "lambda.js", "app.js"):
            js_path = os.path.join(root, js_name)
            if os.path.isfile(js_path):
                try:
                    with open(js_path, "r", encoding="utf-8", errors="ignore") as fh:
                        content = fh.read()
                        if "exports.handler" in content or "module.exports.handler" in content:
                            has_lambda_marker = True
                            break
                except Exception:
                    pass
    if has_lambda_marker and not has_frontend_marker and not has_backend_marker:
        return "lambda_api"

    # Pure static site — has index.html AND no recognized frontend/backend framework
    # This catches repos with package.json for build tools (gulp, grunt) but no app framework
    # Check root, dist/, src/ for index.html
    _has_index = (
        _exists(root, "index.html")
        or os.path.isfile(os.path.join(root, "dist", "index.html"))
        or os.path.isfile(os.path.join(root, "src", "index.html"))
    )
    if _has_index and not has_frontend_marker and not has_backend_marker:
        return "static_site"

    # SAM / serverless
    if _exists(root, "template.yaml") or _exists(root, "template.yml") or _exists(root, "serverless.yml"):
        return "node_api" if "node" in languages else "python_api"

    return "node_api" if "node" in languages else "python_api"


def _detect_docker(root: str) -> dict:
    return {
        "dockerfile": _exists(root, "Dockerfile"),
        "docker_compose": _exists(root, "docker-compose.yml") or _exists(root, "docker-compose.yaml"),
    }


def _deployment_targets(repo_type: str, docker: dict) -> list[str]:
    targets: list[str] = []
    if repo_type == "static_site":
        targets = ["s3_cloudfront"]
    elif repo_type == "frontend_app":
        targets = ["s3_cloudfront", "amplify"]
    elif repo_type == "nextjs_app":
        targets = ["lambda_edge", "ecs_fargate"]
    elif repo_type == "node_backend":
        targets = ["ec2", "ecs_fargate"]
    elif repo_type == "lambda_api":
        targets = ["lambda"]
    elif repo_type == "node_api" or repo_type == "python_api":
        targets = ["lambda", "ecs_fargate"]
    elif repo_type == "docker_app":
        targets = ["ecs_fargate", "ec2"]
    else:
        targets = ["lambda", "ecs_fargate", "ec2"]

    return targets


def _repo_health(root: str) -> dict:
    return {
        "has_readme": _exists(root, "README.md") or _exists(root, "readme.md"),
        "has_tests": any(
            _exists(root, d) for d in ("tests", "test", "__tests__", "spec")
        ),
        "has_ci": any(
            _exists(root, p)
            for p in (".github/workflows", ".gitlab-ci.yml", "Jenkinsfile", "buildspec.yml", ".circleci")
        ),
        "has_license": _exists(root, "LICENSE") or _exists(root, "LICENSE.md"),
        "has_env_file": _exists(root, ".env"),
    }


def _security_scan(root: str, max_files: int = 200) -> list[dict]:
    """Best-effort scan for leaked secrets.  Only checks small text files."""
    warnings: list[dict[str, Any]] = []
    count = 0
    for dirpath, _dirs, filenames in os.walk(root):
        # skip hidden / vendor dirs
        parts = dirpath.replace("\\", "/").split("/")
        if any(p.startswith(".") or p in ("node_modules", "vendor", "__pycache__", ".git") for p in parts):
            continue
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _SECRET_SCAN_EXTENSIONS:
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                size = os.path.getsize(fpath)
                if size > 500_000:
                    continue  # skip large files
                with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                    for line_no, line in enumerate(fh, 1):
                        for pat in _SECRETS_PATTERNS:
                            if pat.search(line):
                                warnings.append({
                                    "file": os.path.relpath(fpath, root),
                                    "line": line_no,
                                    "pattern": pat.pattern[:40],
                                })
            except Exception:
                pass
            count += 1
            if count >= max_files:
                return warnings
    return warnings


# ---------------------------------------------------------------------------
# Build system detection
# ---------------------------------------------------------------------------

_BUILD_SYSTEMS: dict[str, dict] = {
    "node": {
        "type": "node",
        "install": "npm install",
        "build": "npm run build",
        "output_dirs": ["build", "dist", ".next", "out", "public"],
    },
    "python": {
        "type": "python",
        "install": "pip install -r requirements.txt",
        "build": None,
        "output_dirs": [],
    },
    "docker": {
        "type": "docker",
        "install": None,
        "build": "docker build -t app .",
        "output_dirs": [],
    },
    "static": {
        "type": "static",
        "install": None,
        "build": None,
        "output_dirs": ["."],
    },
}


def _detect_build_system(languages: list[str], framework: str | None, docker: dict, repo_type: str) -> dict:
    """Determine the appropriate build system based on project analysis."""
    if repo_type == "static_site":
        return dict(_BUILD_SYSTEMS["static"])

    if repo_type == "lambda_api":
        return {"type": "node", "install": "npm install", "build": None, "output_dirs": ["."]}

    if docker.get("dockerfile"):
        result = dict(_BUILD_SYSTEMS["docker"])
        # If it's also a Node project, include npm install before docker build
        if "node" in languages:
            result["install"] = "npm install"
        return result

    if "node" in languages:
        result = dict(_BUILD_SYSTEMS["node"])
        if framework == "nextjs":
            result["build"] = "npm run build"
            result["output_dirs"] = [".next", "out"]
        return result

    if "python" in languages:
        return dict(_BUILD_SYSTEMS["python"])

    return dict(_BUILD_SYSTEMS["static"])


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def _compute_confidence(languages: list[str], framework: str | None, repo_type: str, docker: dict) -> float:
    """Compute a 0.0–1.0 confidence score for the analysis."""
    score = 0.0

    # Language detected → +0.25
    if languages and languages[0] != "unknown":
        score += 0.25

    # Framework detected → +0.30
    if framework:
        score += 0.30

    # Repo type is specific (not default backend_api) → +0.20
    if repo_type in ("static_site", "frontend_app", "nextjs_app", "serverless_app", "fullstack", "monorepo", "node_backend", "lambda_api"):
        score += 0.20
    elif repo_type == "backend_api" and framework:
        score += 0.15

    # Docker present → +0.15
    if docker.get("dockerfile"):
        score += 0.15

    # Multiple languages → slight penalty (ambiguity)
    if len(languages) > 2:
        score -= 0.05

    return round(min(max(score, 0.1), 1.0), 2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_repo(root: str) -> dict:
    """Analyze a repository directory and return a structured report.

    Parameters
    ----------
    root : str
        Absolute path to the repository root.

    Returns
    -------
    dict
        Structured analysis with keys: language, languages, framework, repo_type,
        dockerized, docker, deployment_targets, build_system, confidence_score,
        health, security_warnings.
    """
    if not os.path.isdir(root):
        raise ValueError(f"Repository root does not exist: {root}")

    languages = _detect_languages(root)
    framework = _detect_framework(root, languages)
    repo_type = _detect_repo_type(root, languages, framework)
    docker = _detect_docker(root)
    targets = _deployment_targets(repo_type, docker)
    health = _repo_health(root)
    security = _security_scan(root)
    build_system = _detect_build_system(languages, framework, docker, repo_type)
    confidence = _compute_confidence(languages, framework, repo_type, docker)

    return {
        "language": languages[0],
        "languages": languages,
        "framework": framework,
        "repo_type": repo_type,
        "dockerized": docker.get("dockerfile", False),
        "docker": docker,
        "deployment_targets": targets,
        "build_system": build_system,
        "confidence_score": confidence,
        "health": health,
        "security_warnings": security,
    }
