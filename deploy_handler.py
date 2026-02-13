import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

import boto3


def _run(cmd, cwd=None, env=None, timeout=None):
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        shell=False,
    )
    return proc.returncode, proc.stdout


def _which(exe):
    return shutil.which(exe)


def _is_github_http_url(url: str) -> bool:
    try:
        u = urllib.parse.urlparse(url)
        return u.scheme in ("http", "https") and u.netloc.lower() == "github.com"
    except Exception:
        return False


def _github_zip_url(repo_url: str, branch: str) -> str:
    repo_url = repo_url.rstrip("/")
    if repo_url.endswith(".git"):
        repo_url = repo_url[:-4]
    return f"{repo_url}/archive/refs/heads/{urllib.parse.quote(branch)}.zip"


def _download(url: str, dest_path: str):
    req = urllib.request.Request(url, headers={"User-Agent": "universal-deployer"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest_path, "wb") as f:
        shutil.copyfileobj(resp, f)


def _extract_zip(zip_path: str, dest_dir: str) -> str:
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(dest_dir)
    entries = [p for p in os.listdir(dest_dir) if os.path.isdir(os.path.join(dest_dir, p))]
    if len(entries) != 1:
        raise RuntimeError(f"Unexpected zip structure: {entries}")
    return os.path.join(dest_dir, entries[0])


def _detect_project(root: str) -> dict:
    markers = {
        "node": os.path.exists(os.path.join(root, "package.json")),
        "python": os.path.exists(os.path.join(root, "requirements.txt"))
        or os.path.exists(os.path.join(root, "pyproject.toml")),
        "java_maven": os.path.exists(os.path.join(root, "pom.xml")),
        "java_gradle": os.path.exists(os.path.join(root, "build.gradle"))
        or os.path.exists(os.path.join(root, "build.gradle.kts")),
        "sam": os.path.exists(os.path.join(root, "template.yaml"))
        or os.path.exists(os.path.join(root, "template.yml"))
        or os.path.exists(os.path.join(root, "samconfig.toml")),
    }

    framework = None
    if markers["node"]:
        try:
            with open(os.path.join(root, "package.json"), "r", encoding="utf-8") as f:
                pkg = json.load(f)
            deps = {}
            deps.update(pkg.get("dependencies", {}) or {})
            deps.update(pkg.get("devDependencies", {}) or {})
            if "react-scripts" in deps or "react" in deps:
                framework = "react"
            elif "vue" in deps or "@vue/cli-service" in deps:
                framework = "vue"
        except Exception:
            framework = "node"

    return {"markers": markers, "framework": framework}


def _ensure_tools_for_build(detection: dict) -> list:
    missing = []
    if detection["markers"].get("node") and not _which("npm"):
        missing.append("npm")
    if detection["markers"].get("sam") and not _which("sam"):
        missing.append("sam")
    if not _which("python"):
        missing.append("python")
    return missing


def _build_frontend(root: str, framework: str) -> str | None:
    if not framework:
        return None

    # npm install
    rc, out = _run(["npm", "ci"], cwd=root)
    if rc != 0:
        rc, out2 = _run(["npm", "install"], cwd=root)
        out += "\n" + out2
        if rc != 0:
            raise RuntimeError("npm install failed:\n" + out)

    rc, out = _run(["npm", "run", "build"], cwd=root)
    if rc != 0:
        raise RuntimeError("npm run build failed:\n" + out)

    for cand in ("build", "dist"):
        p = os.path.join(root, cand)
        if os.path.isdir(p):
            return p
    return None


def _upload_static_site(build_dir: str, bucket_prefix: str) -> dict:
    s3 = boto3.client("s3")

    h = hashlib.sha256(build_dir.encode("utf-8")).hexdigest()[:10]
    bucket = f"{bucket_prefix}-{h}".lower()

    region = os.environ.get("AWS_REGION") or boto3.session.Session().region_name
    create_kwargs = {"Bucket": bucket}
    if region and region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}

    try:
        s3.create_bucket(**create_kwargs)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass

    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": False,
            "RestrictPublicBuckets": False,
        },
    )

    s3.put_bucket_website(
        Bucket=bucket,
        WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}, "ErrorDocument": {"Key": "index.html"}},
    )

    # Upload files
    for dirpath, _, filenames in os.walk(build_dir):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, build_dir).replace("\\", "/")
            extra = {}
            if rel.endswith(".html"):
                extra["ContentType"] = "text/html"
            elif rel.endswith(".js"):
                extra["ContentType"] = "application/javascript"
            elif rel.endswith(".css"):
                extra["ContentType"] = "text/css"

            s3.upload_file(full, bucket, rel, ExtraArgs=extra or None)

    website_url = f"http://{bucket}.s3-website-{region}.amazonaws.com" if region else f"http://{bucket}.s3-website.amazonaws.com"
    return {"bucket": bucket, "website_url": website_url}


def _deploy_backend_as_lambda(root: str, function_name_prefix: str) -> dict | None:
    # Best-effort: if repo is already a SAM app, we try sam deploy; otherwise skip.
    if not (os.path.exists(os.path.join(root, "template.yaml")) or os.path.exists(os.path.join(root, "template.yml"))):
        return None

    if not _which("sam"):
        raise RuntimeError("SAM CLI not available in this runtime; backend deploy requires SAM CLI")

    stack = f"{function_name_prefix}-backend"
    rc, out = _run(["sam", "build"], cwd=root)
    if rc != 0:
        raise RuntimeError("sam build failed:\n" + out)

    rc, out = _run(
        [
            "sam",
            "deploy",
            "--stack-name",
            stack,
            "--resolve-s3",
            "--capabilities",
            "CAPABILITY_IAM",
            "CAPABILITY_AUTO_EXPAND",
            "--no-confirm-changeset",
            "--no-fail-on-empty-changeset",
        ],
        cwd=root,
    )
    if rc != 0:
        raise RuntimeError("sam deploy failed:\n" + out)

    cfn = boto3.client("cloudformation")
    outs = cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])
    outputs = {o["OutputKey"]: o["OutputValue"] for o in outs}
    return {"stack": stack, "outputs": outputs}


def lambda_handler(event, context):
    repo_url = os.environ.get("REPO_URL") or (event.get("RepoUrl") if isinstance(event, dict) else None)
    branch = os.environ.get("REPO_BRANCH") or (event.get("Branch") if isinstance(event, dict) else "main")

    if not repo_url:
        return {"statusCode": 400, "body": json.dumps({"error": "RepoUrl is required"})}

    if not _is_github_http_url(repo_url):
        return {"statusCode": 400, "body": json.dumps({"error": "Only https://github.com/<owner>/<repo> URLs are supported"})}

    with tempfile.TemporaryDirectory() as td:
        zip_path = os.path.join(td, "repo.zip")
        src_dir = os.path.join(td, "src")
        os.makedirs(src_dir, exist_ok=True)

        zip_url = _github_zip_url(repo_url, branch)
        _download(zip_url, zip_path)
        repo_root = _extract_zip(zip_path, src_dir)

        detection = _detect_project(repo_root)
        missing = _ensure_tools_for_build(detection)
        if missing:
            return {
                "statusCode": 500,
                "body": json.dumps(
                    {
                        "error": "Required build tools missing in runtime",
                        "missing": missing,
                        "note": "Run deploy_handler.py from CodeBuild or an environment with npm/sam installed.",
                    }
                ),
            }

        results = {
            "repo": {"url": repo_url, "branch": branch},
            "detected": detection,
        }

        # Frontend
        if detection["framework"] in ("react", "vue"):
            build_dir = _build_frontend(repo_root, detection["framework"])
            if build_dir:
                results["frontend"] = _upload_static_site(build_dir, bucket_prefix="universal-deployer")

        # Backend
        backend = _deploy_backend_as_lambda(repo_root, function_name_prefix="universal-deployer")
        if backend:
            results["backend"] = backend

        return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": json.dumps(results)}
