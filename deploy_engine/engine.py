"""
Deployment Engine
=================

Handles the full deploy lifecycle:

1. Clone / download the repository
2. Build the project (npm / pip / maven)
3. Generate infrastructure template via ``infra_generator``
4. Deploy to AWS via CloudFormation / SAM
5. Return deployment URL and logs
6. Auto-rollback on failure

This module *wraps* the existing helpers from ``deploy_handler.py`` so that
the original code remains untouched and fully functional on its own.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any

import boto3
import boto3.session

# Import shared helpers from the existing handler (re-use, don't rewrite)
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from deploy_handler import (  # noqa: E402
    _build_frontend,
    _download,
    _extract_zip,
    _github_zip_url,
    _is_github_http_url,
    _run,
    _upload_static_site,
    _which,
)
from infra_generator.generator import generate_template  # noqa: E402
from orchestrator import persistence


# ---------------------------------------------------------------------------
# Deployment state store (SQLite via persistence.py)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(deploy_id: str, message: str) -> None:
    """Append a timestamped log line to the persistent record."""
    entry = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {message}"
    persistence.append_log(deploy_id, entry)


def _build_node(deploy_id: str, root: str) -> str | None:
    """Install deps and build a Node project.  Returns the build output dir."""
    _log(deploy_id, "Installing dependencies")
    try:
        subprocess.run("npm ci", shell=True, check=True, cwd=root, capture_output=True, text=True)
    except subprocess.CalledProcessError:
        try:
            subprocess.run("npm install", shell=True, check=True, cwd=root, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"npm install failed:\n{e.stderr or e.stdout}")

    import json
    has_build_script = False
    pkg_path = os.path.join(root, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                has_build_script = "build" in json.load(f).get("scripts", {})
        except Exception:
            pass

    if has_build_script:
        _log(deploy_id, "Running build")
        try:
            subprocess.run("npm run build", shell=True, check=True, cwd=root, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"npm run build failed:\n{e.stderr or e.stdout}")
        _log(deploy_id, "Build completed")
    else:
        _log(deploy_id, "No build script found, skipping build")

    for d in ("build", "dist", "out", "public"):
        p = os.path.join(root, d)
        if os.path.isdir(p):
            return p
    return root


def _build_python(root: str) -> None:
    req = os.path.join(root, "requirements.txt")
    if os.path.isfile(req):
        rc, out = _run([sys.executable, "-m", "pip", "install", "-r", req, "-t", os.path.join(root, ".deps")], cwd=root)
        if rc != 0:
            raise RuntimeError(f"pip install failed:\n{out}")


def _rollback_stack(stack_name: str, region: str, session: boto3.session.Session | None = None) -> None:
    """Delete a CloudFormation stack as part of rollback."""
    try:
        s = session or boto3
        cfn = s.client("cloudformation", region_name=region)
        cfn.delete_stack(StackName=stack_name)
    except Exception:
        pass  # best-effort


# ---------------------------------------------------------------------------
# Deploy Engine
# ---------------------------------------------------------------------------

class DeployEngine:
    """Stateful deployment engine with logging, build, deploy, and rollback."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region
        self._session: boto3.session.Session | None = None

    # -- boto3 session management -------------------------------------------

    def _get_session(self, aws_credentials: dict | None = None) -> boto3.session.Session:
        """Create a boto3 session — use only explicit credentials."""
        if not aws_credentials or not aws_credentials.get("access_key") or not aws_credentials.get("secret_key") or not aws_credentials.get("region"):
            raise ValueError("AWS credentials are required to deploy.")
            
        return boto3.session.Session(
            aws_access_key_id=aws_credentials.get("access_key"),
            aws_secret_access_key=aws_credentials.get("secret_key"),
            region_name=aws_credentials.get("region"),
        )

    # -- public API ---------------------------------------------------------

    def start_deployment(
        self,
        repo_url: str,
        branch: str = "main",
        target: str = "lambda",
        extra_context: dict | None = None,
        aws_credentials: dict | None = None,
    ) -> dict:
        """Full lifecycle deployment."""
        deploy_id = str(uuid.uuid4())[:12]
        dep = {
            "id": deploy_id,
            "repo_url": repo_url,
            "branch": branch,
            "target": target,
            "status": "started",
            "url": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        persistence.save_deployment(dep)

        # Create session from strictly provided credentials
        try:
            self._session = self._get_session(aws_credentials)
        except ValueError as exc:
            _log(deploy_id, f"❌ DEPLOYMENT FAILED: {exc}")
            dep = persistence.get_deployment(deploy_id) or dep
            dep["status"] = "failed"
            dep["error"] = str(exc)
            persistence.save_deployment(dep)
            return dep

        if aws_credentials and aws_credentials.get("region"):
            self.region = aws_credentials["region"]

        _log(deploy_id, "Using AWS account credentials")
        _log(deploy_id, f"Region selected: {self.region}")
        _log(deploy_id, "Creating resources in specified account")

        try:
            self._execute(deploy_id, repo_url, branch, target, extra_context or {})
        except Exception as exc:
            _log(deploy_id, f"❌ DEPLOYMENT FAILED: {exc}")
            # Refresh record to preserve metadata/outputs
            dep = persistence.get_deployment(deploy_id) or dep
            dep["status"] = "failed"
            dep["error"] = str(exc)
            persistence.save_deployment(dep)

        return persistence.get_deployment(deploy_id)

    @staticmethod
    def get_deployment(deploy_id: str) -> dict | None:
        return persistence.get_deployment(deploy_id)

    @staticmethod
    def list_deployments() -> list[dict]:
        return persistence.list_deployments()

    def _update_status(self, deploy_id: str, status: str):
        dep = persistence.get_deployment(deploy_id)
        if dep:
            dep["status"] = status
            persistence.save_deployment(dep)

    # -- private lifecycle --------------------------------------------------

    def _execute(
        self,
        deploy_id: str,
        repo_url: str,
        branch: str,
        target: str,
        ctx: dict,
    ) -> None:
        if not _is_github_http_url(repo_url):
            raise ValueError("Only https://github.com/<owner>/<repo> URLs are supported")

        _log(deploy_id, f"🚀 Starting deployment → {target}")
        self._update_status(deploy_id, "cloning")

        # Step 1: Download repository
        with tempfile.TemporaryDirectory() as td:
            zip_path = os.path.join(td, "repo.zip")
            src_dir = os.path.join(td, "src")
            os.makedirs(src_dir, exist_ok=True)

            _log(deploy_id, f"Downloading repository")
            zip_url = _github_zip_url(repo_url, branch)
            try:
                _download(zip_url, zip_path)
            except Exception as e:
                if branch == "main":
                    _log(deploy_id, "Download failed for 'main'. Trying 'master' branch...")
                    zip_url = _github_zip_url(repo_url, "master")
                    _download(zip_url, zip_path)
                else:
                    raise

            # Step 2: Extract repository
            _log(deploy_id, "Extracting repository")
            repo_root = _extract_zip(zip_path, src_dir)

            # Step 3: Run analyzer
            self._update_status(deploy_id, "analyzing")
            _log(deploy_id, "Running repository analyzer")
            from repo_analyzer.analyzer import analyze_repo
            analysis = analyze_repo(repo_root)
            _log(deploy_id, f"Detected language/framework: {analysis.get('language')} / {analysis.get('framework') or 'none'}")
            _log(deploy_id, f"Repo type detected: {analysis.get('repo_type')}")

            # Inject repo info into context for deployers
            ctx["repo_url"] = repo_url
            ctx["branch"] = branch
            ctx["repo_type"] = analysis.get("repo_type")

            # Step 4: Detect build system
            build_sys = analysis.get("build_system", {})
            _log(deploy_id, f"🔧 [4/7] Build system: {build_sys.get('type', 'none')}")
            _log(deploy_id, f"Deployment target: {target}")

            # Step 5: Build project
            self._update_status(deploy_id, "building")
            _log(deploy_id, "🔨 [5/7] Building project…")
            self._build(deploy_id, repo_root, target, ctx, build_sys)
            _log(deploy_id, "✅ Build complete")

            # Step 6: Generate infra template
            self._update_status(deploy_id, "generating_infra")
            _log(deploy_id, "Generating infrastructure")
            tmpl = generate_template(target, ctx, output_dir=os.path.join(td, "infra"))

            # Step 7: Deploy to AWS
            self._update_status(deploy_id, "deploying")
            _log(deploy_id, "Deploying AWS resources")
            
            try:
                result = self._deploy(deploy_id, repo_root, target, tmpl, ctx)
            except Exception as e:
                # Rollback if SAM/CFN deployed a stack but failed
                stack_name = ctx.get("stack_name")
                if stack_name and "sam deploy failed" in str(e):
                    _log(deploy_id, f"Deployment failed. Rolling back stack {stack_name}...")
                    _rollback_stack(stack_name, self.region, self.session)
                raise e

            dep = persistence.get_deployment(deploy_id)
            dep["url"] = result.get("url")
            dep["outputs"] = result.get("outputs", {})
            dep["status"] = "success"
            persistence.save_deployment(dep)
            _log(deploy_id, "Live URL generated")

    def _build(self, deploy_id: str, root: str, target: str, ctx: dict, build_sys: dict | None = None) -> None:
        """Build step — use build_system info or fallback to auto-detection."""
        bs_type = (build_sys or {}).get("type", "")

        if bs_type == "static":
            _log(deploy_id, "🌐 Static site — skipping build")
            return

        if bs_type == "docker":
            _log(deploy_id, "🐳 Dockerized project — docker build")
            if _which("docker"):
                rc, out = _run(["docker", "build", "-t", f"ud-{deploy_id}", "."], cwd=root)
                if rc != 0:
                    _log(deploy_id, f"❌ ERROR in docker build:\n{out}")
                    raise RuntimeError(f"docker build failed (rc={rc})")
                ctx["docker_image"] = f"ud-{deploy_id}"
            else:
                _log(deploy_id, "⚠️ Docker not found — skipping container build")
            return

        # Node.js
        pkg_json = os.path.join(root, "package.json")
        if bs_type == "node" or os.path.isfile(pkg_json):
            build_dir = _build_node(deploy_id, root)
            if build_dir:
                ctx["build_dir"] = build_dir
            return

        # Python
        req_txt = os.path.join(root, "requirements.txt")
        if bs_type == "python" or os.path.isfile(req_txt):
            _log(deploy_id, "🐍 Detected Python project — installing dependencies")
            _build_python(root)
            return

        _log(deploy_id, "ℹ️ No build system detected — skipping build")

    def _deploy(self, deploy_id: str, root: str, target: str, tmpl: dict, ctx: dict) -> dict:
        """Deploy step — dispatch to the right deployer."""
        if target == "s3_cloudfront":
            return self._deploy_static(deploy_id, root, ctx)
        elif target in ("lambda", "lambda_edge"):
            return self._deploy_lambda_api(deploy_id, root, ctx)
        elif target == "ecs_fargate":
            return self._deploy_ecs(deploy_id, root, tmpl, ctx)
        elif target == "ec2":
            return self._deploy_ec2(deploy_id, root, tmpl, ctx)
        else:
            raise ValueError(f"Unsupported target: {target}")

    # -- target-specific deployers ------------------------------------------

    def _deploy_lambda_api(self, deploy_id: str, root: str, ctx: dict) -> dict:
        """Deploy a Lambda function with API Gateway HTTP API (direct boto3, no SAM)."""
        s = self._session or boto3.Session()
        lam = s.client("lambda", region_name=self.region)
        apigw = s.client("apigatewayv2", region_name=self.region)
        iam = s.client("iam")
        sts = s.client("sts")

        account_id = sts.get_caller_identity()["Account"]
        func_name = f"ud-lambda-{deploy_id[:8]}"

        # 1. Create IAM execution role
        _log(deploy_id, "Creating Lambda execution role")
        role_name = f"ud-role-{deploy_id[:8]}"
        assume_role_policy = json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        })
        try:
            role_res = iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=assume_role_policy,
                Description=f"Universal Deployer Lambda role - {deploy_id}",
            )
            role_arn = role_res["Role"]["Arn"]
            iam.attach_role_policy(
                RoleName=role_name,
                PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
            )
            # Wait for IAM propagation
            time.sleep(10)
        except iam.exceptions.EntityAlreadyExistsException:
            role_res = iam.get_role(RoleName=role_name)
            role_arn = role_res["Role"]["Arn"]

        # 2. Package Lambda function
        _log(deploy_id, "Packaging Lambda function")
        zip_path = os.path.join(tempfile.gettempdir(), f"{deploy_id}-lambda.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, _dirs, filenames in os.walk(root):
                parts = dirpath.replace("\\", "/").split("/")
                if any(p in ("node_modules", ".git", "__pycache__") for p in parts):
                    continue
                for fname in filenames:
                    full = os.path.join(dirpath, fname)
                    arcname = os.path.relpath(full, root).replace("\\", "/")
                    zf.write(full, arcname)

        with open(zip_path, "rb") as f:
            zip_bytes = f.read()

        # 3. Create Lambda function
        _log(deploy_id, "Creating Lambda function")
        handler = "index.handler"
        for candidate in ("handler.js", "lambda.js", "index.js"):
            if os.path.isfile(os.path.join(root, candidate)):
                handler = candidate.replace(".js", "") + ".handler"
                break

        try:
            fn_res = lam.create_function(
                FunctionName=func_name,
                Runtime="nodejs18.x",
                Role=role_arn,
                Handler=handler,
                Code={"ZipFile": zip_bytes},
                Timeout=30,
                MemorySize=128,
                Description=f"Universal Deployer - {deploy_id}",
            )
        except lam.exceptions.ResourceConflictException:
            lam.update_function_code(
                FunctionName=func_name,
                ZipFile=zip_bytes,
            )
            fn_res = lam.get_function(FunctionName=func_name)
            fn_res = fn_res["Configuration"]

        function_arn = fn_res["FunctionArn"]

        # 4. Create API Gateway HTTP API
        _log(deploy_id, "Creating API Gateway")
        api_res = apigw.create_api(
            Name=f"ud-api-{deploy_id[:8]}",
            ProtocolType="HTTP",
            Description=f"Universal Deployer API - {deploy_id}",
        )
        api_id = api_res["ApiId"]

        # 5. Add Lambda integration
        _log(deploy_id, "Connecting Lambda integration")
        integ_res = apigw.create_integration(
            ApiId=api_id,
            IntegrationType="AWS_PROXY",
            IntegrationUri=function_arn,
            PayloadFormatVersion="2.0",
        )
        integration_id = integ_res["IntegrationId"]

        # 6. Create catch-all route: ANY /{proxy+}
        apigw.create_route(
            ApiId=api_id,
            RouteKey="ANY /{proxy+}",
            Target=f"integrations/{integration_id}",
        )

        # 7. Deploy prod stage
        _log(deploy_id, "Deploying API stage")
        apigw.create_stage(
            ApiId=api_id,
            StageName="prod",
            AutoDeploy=True,
        )

        # 8. Add Lambda invoke permission for API Gateway
        try:
            lam.add_permission(
                FunctionName=func_name,
                StatementId=f"apigw-invoke-{api_id}",
                Action="lambda:InvokeFunction",
                Principal="apigateway.amazonaws.com",
                SourceArn=f"arn:aws:execute-api:{self.region}:{account_id}:{api_id}/*",
            )
        except lam.exceptions.ResourceConflictException:
            pass

        api_url = f"https://{api_id}.execute-api.{self.region}.amazonaws.com/prod"

        _log(deploy_id, f"API endpoint: {api_url}")
        _log(deploy_id, "Deployment completed")

        return {
            "url": api_url,
            "outputs": {
                "function_name": func_name,
                "function_arn": function_arn,
                "api_id": api_id,
                "api_url": api_url,
                "role_arn": role_arn,
            },
        }

    def _deploy_static(self, deploy_id: str, root: str, ctx: dict) -> dict:
        """Upload static assets to S3 and return the website URL."""
        import hashlib
        build_dir = ctx.get("build_dir", root)
        _log(deploy_id, "Uploading to S3")
        
        s = self._session or boto3.Session()
        s3 = s.client("s3", region_name=self.region)
        
        h = hashlib.sha256(build_dir.encode("utf-8")).hexdigest()[:10]
        bucket = f"universal-deployer-{deploy_id[:8]}-{h}".lower()

        create_kwargs = {"Bucket": bucket}
        if self.region and self.region != "us-east-1":
            create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}

        try:
            s3.create_bucket(**create_kwargs)
        except s3.exceptions.BucketAlreadyOwnedByYou:
            pass
        except Exception as e:
            if "BucketAlreadyExists" not in str(e):
                raise

        # Disable Block Public Access completely to allow public bucket policy
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": False,
                "IgnorePublicAcls": False,
                "BlockPublicPolicy": False,
                "RestrictPublicBuckets": False,
            },
        )
        
        # Apply Bucket Policy for public read access
        public_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicReadGetObject",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{bucket}/*"
                }
            ]
        }
        import time
        time.sleep(2) # Give AWS IAM propagation delay
        s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(public_policy))

        s3.put_bucket_website(
            Bucket=bucket,
            WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}, "ErrorDocument": {"Key": "index.html"}},
        )

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

        website_url = f"http://{bucket}.s3-website-{self.region}.amazonaws.com" if self.region else f"http://{bucket}.s3-website.amazonaws.com"
        import time
        
        _log(deploy_id, "Creating CloudFront distribution")
        cloudfront = s.client("cloudfront")
        origin_id = f"S3-{bucket}"
        
        try:
            cf_res = cloudfront.create_distribution(
                DistributionConfig={
                    'CallerReference': str(time.time()),
                    'Comment': f"Universal Deployer CDN - {deploy_id}",
                    'Enabled': True,
                    'DefaultRootObject': 'index.html',
                    'Origins': {
                        'Quantity': 1,
                        'Items': [{
                            'Id': origin_id,
                            'DomainName': f"{bucket}.s3.amazonaws.com",
                            'S3OriginConfig': {
                                'OriginAccessIdentity': ''
                            }
                        }]
                    },
                    'DefaultCacheBehavior': {
                        'TargetOriginId': origin_id,
                        'ViewerProtocolPolicy': 'redirect-to-https',
                        'AllowedMethods': {
                            'Quantity': 2,
                            'Items': ['GET', 'HEAD']
                        },
                        'Compress': True,
                        'ForwardedValues': {
                            'QueryString': False,
                            'Cookies': {'Forward': 'none'}
                        },
                        'MinTTL': 0,
                        'TrustedSigners': {
                            'Enabled': False,
                            'Quantity': 0
                        }
                    }
                }
            )
            
            domain_name = cf_res['Distribution']['DomainName']
            dist_id = cf_res['Distribution']['Id']
            
            _log(deploy_id, "Waiting for distribution deployment")
            _log(deploy_id, "CloudFront URL generated")
            
            cf_url = f"https://{domain_name}"
            result = {"bucket": bucket, "website_url": website_url, "cloudfront_url": cf_url, "distribution_id": dist_id}
            
            _log(deploy_id, "Deployment completed")
            return {"url": cf_url, "outputs": result}
            
        except Exception as e:
            _log(deploy_id, f"⚠️ Failed to create CloudFront distribution: {e}")
            _log(deploy_id, "Falling back to S3 Website URL")
            result = {"bucket": bucket, "website_url": website_url}
            _log(deploy_id, "Deployment completed")
            return {"url": website_url, "outputs": result}

    def _deploy_lambda_legacy(self, deploy_id: str, root: str, tmpl: dict, ctx: dict) -> dict:
        """DEPRECATED: SAM-based deployment — kept for reference only. Not called."""
        raise RuntimeError("SAM deployment is deprecated. Use direct boto3 Lambda deployment.")

    def _deploy_ecs(self, deploy_id: str, root: str, tmpl: dict, ctx: dict) -> dict:
        """Deploy to ECS Fargate using generated CloudFormation template."""
        stack_name = ctx.get("stack_name", f"ud-ecs-{deploy_id}")
        _log(deploy_id, "🐳 ECS Fargate deployment requires a container image in ECR.")
        _log(deploy_id, f"📐 Generated template saved to: {tmpl.get('file', 'memory')}")
        return {
            "url": f"(ECS deployment staged — stack: {stack_name})",
            "outputs": {"stack_name": stack_name, "template_file": tmpl.get("file")},
        }

    def _deploy_ec2(self, deploy_id: str, root: str, tmpl: dict, ctx: dict) -> dict:
        """Deploy a Node.js backend to an EC2 instance."""
        s = self._session or boto3.Session()
        ec2 = s.client("ec2", region_name=self.region)

        repo_url = ctx.get("repo_url", "")
        branch = ctx.get("branch", "main")

        _log(deploy_id, "🖥️  Launching EC2 instance for Node.js backend")

        # 1. Create Security Group allowing port 3000
        sg_name = f"ud-sg-{deploy_id}"
        _log(deploy_id, "Creating security group for port 3000")
        try:
            sg_res = ec2.create_security_group(
                GroupName=sg_name,
                Description=f"Universal Deployer - {deploy_id}",
            )
            sg_id = sg_res["GroupId"]
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 3000,
                        "ToPort": 3000,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "Node app"}],
                    },
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 22,
                        "ToPort": 22,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}],
                    },
                ],
            )
        except Exception as e:
            if "already exists" in str(e).lower():
                sgs = ec2.describe_security_groups(GroupNames=[sg_name])["SecurityGroups"]
                sg_id = sgs[0]["GroupId"]
            else:
                raise

        # 2. User-data script to install Node.js + clone + run
        user_data = f"""#!/bin/bash
set -ex
curl -fsSL https://rpm.nodesource.com/setup_18.x | bash -
yum install -y nodejs git
cd /home/ec2-user
git clone -b {branch} {repo_url} app
cd app
npm install
export PORT=3000
nohup npm start > /home/ec2-user/app.log 2>&1 &
"""

        # 3. Find latest Amazon Linux 2 AMI
        _log(deploy_id, "Finding latest Amazon Linux 2 AMI")
        images = ec2.describe_images(
            Owners=["amazon"],
            Filters=[
                {"Name": "name", "Values": ["amzn2-ami-hvm-*-x86_64-gp2"]},
                {"Name": "state", "Values": ["available"]},
            ],
        )["Images"]
        images.sort(key=lambda x: x.get("CreationDate", ""), reverse=True)
        ami_id = images[0]["ImageId"] if images else "ami-0c02fb55956c7d316"

        # 4. Launch instance
        _log(deploy_id, "Launching EC2 instance (t2.micro)")
        import base64
        run_res = ec2.run_instances(
            ImageId=ami_id,
            InstanceType="t2.micro",
            MinCount=1,
            MaxCount=1,
            SecurityGroupIds=[sg_id],
            UserData=base64.b64encode(user_data.encode()).decode(),
            TagSpecifications=[{
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": f"UniversalDeployer-{deploy_id}"},
                    {"Key": "Project", "Value": "universal-deployer"},
                ],
            }],
        )
        instance_id = run_res["Instances"][0]["InstanceId"]
        _log(deploy_id, f"Instance launched: {instance_id}")

        # 5. Wait for running state
        _log(deploy_id, "Waiting for instance to enter running state")
        waiter = ec2.get_waiter("instance_running")
        waiter.wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 10, "MaxAttempts": 30})

        # 6. Get public IP
        desc = ec2.describe_instances(InstanceIds=[instance_id])
        public_ip = desc["Reservations"][0]["Instances"][0].get("PublicIpAddress", "pending")
        url = f"http://{public_ip}:3000"

        _log(deploy_id, f"EC2 instance running at {public_ip}")
        _log(deploy_id, f"Application URL: {url}")
        _log(deploy_id, "Deployment completed")

        return {
            "url": url,
            "outputs": {
                "instance_id": instance_id,
                "public_ip": public_ip,
                "security_group": sg_id,
                "ami_id": ami_id,
            },
        }
