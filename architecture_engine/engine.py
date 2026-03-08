"""
Architecture Decision Engine
=============================

Accepts the structured output of ``repo_analyzer.analyze_repo()`` and returns
ranked architecture options with cost estimation and AI-advisor reasoning.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Architecture option templates
# ---------------------------------------------------------------------------

_ARCH_DB: dict[str, dict[str, Any]] = {
    "s3_cloudfront": {
        "name": "S3 + CloudFront (Static Hosting)",
        "aws_services": ["S3", "CloudFront", "Route 53"],
        "cost_tier": "very_low",
        "complexity": 1,
        "scalability": "high",
        "cold_start": False,
        "best_for": "Static websites and single-page applications",
    },
    "lambda": {
        "name": "Lambda + API Gateway (Serverless)",
        "aws_services": ["Lambda", "API Gateway", "CloudWatch", "IAM"],
        "cost_tier": "low",
        "complexity": 2,
        "scalability": "very_high",
        "cold_start": True,
        "best_for": "Low-to-moderate traffic APIs and event-driven services",
    },
    "lambda_edge": {
        "name": "Lambda + CloudFront (SSR / Next.js)",
        "aws_services": ["Lambda", "CloudFront", "Lambda@Edge", "S3", "IAM"],
        "cost_tier": "low",
        "complexity": 3,
        "scalability": "very_high",
        "cold_start": True,
        "best_for": "Next.js SSR, hybrid static + server-rendered pages",
    },
    "ecs_fargate": {
        "name": "ECS Fargate (Managed Containers)",
        "aws_services": ["ECS", "Fargate", "ALB", "ECR", "CloudWatch"],
        "cost_tier": "medium",
        "complexity": 3,
        "scalability": "high",
        "cold_start": False,
        "best_for": "Containerised workloads, long-running processes, predictable traffic",
    },
    "ec2": {
        "name": "EC2 + Docker (Virtual Machine)",
        "aws_services": ["EC2", "ALB", "ECR", "CloudWatch", "Auto Scaling"],
        "cost_tier": "medium_high",
        "complexity": 4,
        "scalability": "medium",
        "cold_start": False,
        "best_for": "Full control, GPU workloads, legacy apps, custom runtimes",
    },
    "amplify": {
        "name": "AWS Amplify (Managed Frontend Hosting)",
        "aws_services": ["Amplify", "CloudFront", "Route 53"],
        "cost_tier": "low",
        "complexity": 1,
        "scalability": "high",
        "cold_start": False,
        "best_for": "Frontend SPAs with CI/CD built-in",
    },
}

_COST_ESTIMATES: dict[str, str] = {
    "very_low": "~$0–$5/month",
    "low": "~$5–$30/month",
    "medium": "~$30–$100/month",
    "medium_high": "~$50–$200/month",
    "high": "~$200+/month",
}


# ---------------------------------------------------------------------------
# Reasoning engine (AI Deployment Advisor)
# ---------------------------------------------------------------------------

def _build_reasoning(analysis: dict, arch_key: str) -> str:
    """Generate a human-readable explanation for why *arch_key* is recommended."""
    repo_type = analysis.get("repo_type", "")
    framework = analysis.get("framework") or "unknown"
    dockerized = analysis.get("dockerized", False)
    arch = _ARCH_DB[arch_key]

    lines: list[str] = [f"**Recommended architecture: {arch['name']}**\n"]

    # Repo type reasoning
    if repo_type == "static_site":
        lines.append("• The repository is a static website with no server-side logic.")
        lines.append("• Static hosting on S3 + CloudFront provides the lowest cost and highest availability.")
    elif repo_type == "frontend_app":
        lines.append(f"• Detected a **{framework}** frontend application.")
        lines.append("• After building, the output is static assets ideal for CDN-backed hosting.")
    elif repo_type == "nextjs_app":
        if arch_key == "lambda_edge":
            lines.append("• Detected a **Next.js** application with server-side rendering.")
            lines.append("• Lambda@Edge + CloudFront is ideal for Next.js SSR with global CDN caching.")
            lines.append("• Static pages are served from S3, dynamic routes handled by Lambda at the edge.")
        elif arch_key == "s3_cloudfront":
            lines.append("• Detected a **Next.js** application.")
            lines.append("• Using `next export` for fully static output deployed to S3 + CloudFront.")
            lines.append("• This works only if the app has no server-side routes (getServerSideProps).")
        else:
            lines.append(f"• Detected a **Next.js** application deployed via {arch['name']}.")
    elif repo_type in ("backend_api", "serverless_app", "node_api"):
        if arch_key == "lambda":
            lines.append(f"• Detected a **{framework}** backend service.")
            lines.append("• Serverless (Lambda) is ideal for stateless APIs with variable traffic.")
            lines.append("• Pay-per-invocation model keeps costs near zero during low usage.")
        elif arch_key == "ecs_fargate":
            lines.append(f"• Detected a **{framework}** backend service.")
            lines.append("• Fargate is recommended for always-on services or those with longer request times.")
        else:
            lines.append(f"• Detected a **{framework}** backend service.")
    elif repo_type == "node_backend":
        lines.append(f"• Detected a **{framework}** Node.js backend application.")
        if arch_key == "ec2":
            lines.append("• EC2 provides full control for long-running Node.js servers (express, fastify, etc).")
            lines.append("• The application will be deployed on an EC2 instance with Node.js installed.")
            lines.append("• Port 3000 will be exposed for incoming traffic.")
        elif arch_key == "ecs_fargate":
            lines.append("• Fargate provides managed containers for the Node.js backend.")
        else:
            lines.append(f"• Deployed via {arch['name']}.")
    elif repo_type == "lambda_api":
        lines.append("• Detected an **AWS Lambda** API handler pattern.")
        lines.append("• The code contains `exports.handler` — a standard Lambda function entry point.")
        lines.append("• Lambda + API Gateway provides the lowest cost for event-driven APIs.")
        lines.append("• Pay-per-invocation with automatic scaling to zero when idle.")
    elif repo_type == "fullstack":
        lines.append(f"• This is a fullstack application ({framework}).")
        lines.append("• The recommended architecture can serve both frontend assets and backend APIs.")
    elif repo_type == "monorepo":
        lines.append("• Detected a monorepo with multiple services.")
        lines.append("• Container-based deployment allows each service to be packaged independently.")

    # Docker reasoning
    if dockerized:
        lines.append("• A Dockerfile is present — container-based deployment is natural and avoids runtime mismatch.")

    # Cost
    lines.append(f"• Estimated cost: **{_COST_ESTIMATES.get(arch['cost_tier'], 'varies')}** (at moderate traffic).")
    lines.append(f"• Complexity rating: **{arch['complexity']}/5**.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def recommend_architecture(analysis: dict) -> dict:
    """Return architecture recommendations based on repo analysis.

    Parameters
    ----------
    analysis : dict
        Output of ``repo_analyzer.analyze_repo()``.

    Returns
    -------
    dict
        Keys: ``recommended``, ``options`` (list of option dicts), ``reasoning``.
    """
    targets = analysis.get("deployment_targets", [])
    if not targets:
        targets = ["lambda", "ecs_fargate", "ec2"]

    # Pick the first target as recommended
    recommended = targets[0]

    # Build option cards
    options: list[dict[str, Any]] = []
    for t in targets:
        arch = _ARCH_DB.get(t)
        if not arch:
            continue
        options.append({
            "architecture_id": t,
            "name": arch["name"],
            "aws_services": arch["aws_services"],
            "cost_estimate": _COST_ESTIMATES.get(arch["cost_tier"], "varies"),
            "complexity": arch["complexity"],
            "description": arch["best_for"],
            "is_recommended": t == recommended,
            "reasoning": _build_reasoning(analysis, t),
        })

    reasoning = _build_reasoning(analysis, recommended)

    return {
        "recommended": recommended,
        "options": options,
        "reasoning": reasoning,
    }
