# Deployment Runbook

Use this checklist whenever you need to redeploy the Universal Deployer SAM stack. It captures
all commands we validated on 2026-02-13 so you do not have to rediscover the workflow.

## Prerequisites
- AWS CLI and SAM CLI installed on the workstation (PowerShell 5.1 or later).
- AWS credentials configured with permissions to manage Lambda, IAM, CloudFormation, and S3.
- A unique S3 bucket name (global namespace) for SAM artifacts.

## One-Time Bucket Creation
Only create the artifact bucket once per account/region. Replace the bucket name if you need a
different one.

```powershell
aws s3 mb s3://universal-deployer-artifacts-1789 --region us-east-1
```

## Deployment Command Block
Run the following block exactly as-is (or update the variables at the top). It handles directory
changes, build, and conditional deploy so you avoid syntax issues with `&&` on Windows PowerShell.

```powershell
& {
    $workspace = "C:\Users\vishw\cloud-auto-deploy\aws-serverless-cicd-workshop"
    $bucketName = "universal-deployer-artifacts-1789"
    $repoUrl = "https://github.com/aws-samples/aws-serverless-cicd-workshop"
    $branch = "main"

    Set-Location $workspace

    aws s3 ls "s3://$bucketName" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Artifact bucket '$bucketName' is missing or inaccessible."
    }

    sam build --template-file template.yaml
    if ($LASTEXITCODE -eq 0) {
        sam deploy --stack-name universal-deployer `
            --template-file .aws-sam\build\template.yaml `
            --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND `
            --region us-east-1 `
            --no-confirm-changeset `
            --no-fail-on-empty-changeset `
            --parameter-overrides RepoUrl=$repoUrl Branch=$branch `
            --s3-bucket $bucketName
    }
    else {
        throw "sam build failed; deployment skipped."
    }
}
```

## Tips
- Bucket names must only use lowercase letters, numbers, and hyphens—no brackets or spaces.
- If you change the repository or branch, set `$repoUrl` and `$branch` before running the block.
- Keep the `.aws-sam` folder untracked; it only contains build artifacts and will be recreated
  automatically on the next run.
