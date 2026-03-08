"""
Infrastructure Generator
========================

Dynamically generates CloudFormation YAML templates for four deployment
targets.  Templates are stored as Python multi-line strings (no Jinja2
dependency required).

Supported targets:
  - ``s3_cloudfront``   — S3 + CloudFront static hosting
  - ``lambda``          — Lambda + API Gateway (Serverless)
  - ``ecs_fargate``     — ECS Fargate + ALB
  - ``ec2``             — EC2 + Docker (Auto Scaling Group)
"""

from __future__ import annotations

import os
import textwrap
from typing import Any


# ---------------------------------------------------------------------------
# Template builders
# ---------------------------------------------------------------------------

def _s3_cloudfront_template(ctx: dict) -> str:
    bucket = ctx.get("bucket_name", "app-static-hosting")
    return textwrap.dedent(f"""\
    AWSTemplateFormatVersion: '2010-09-09'
    Description: Static site hosting — S3 + CloudFront

    Resources:
      SiteBucket:
        Type: AWS::S3::Bucket
        Properties:
          BucketName: !Sub "{bucket}-${{AWS::AccountId}}"
          WebsiteConfiguration:
            IndexDocument: index.html
            ErrorDocument: index.html
          PublicAccessBlockConfiguration:
            BlockPublicAcls: true
            IgnorePublicAcls: true
            BlockPublicPolicy: false
            RestrictPublicBuckets: false

      BucketPolicy:
        Type: AWS::S3::BucketPolicy
        Properties:
          Bucket: !Ref SiteBucket
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Sid: CloudFrontReadAccess
                Effect: Allow
                Principal:
                  Service: cloudfront.amazonaws.com
                Action: s3:GetObject
                Resource: !Sub "${{SiteBucket.Arn}}/*"
                Condition:
                  StringEquals:
                    AWS:SourceArn: !Sub "arn:aws:cloudfront::${{AWS::AccountId}}:distribution/${{CDN}}"

      OriginAccessControl:
        Type: AWS::CloudFront::OriginAccessControl
        Properties:
          OriginAccessControlConfig:
            Name: !Sub "{bucket}-oac"
            OriginAccessControlOriginType: s3
            SigningBehavior: always
            SigningProtocol: sigv4

      CDN:
        Type: AWS::CloudFront::Distribution
        Properties:
          DistributionConfig:
            Enabled: true
            DefaultRootObject: index.html
            Origins:
              - Id: S3Origin
                DomainName: !GetAtt SiteBucket.RegionalDomainName
                OriginAccessControlId: !GetAtt OriginAccessControl.Id
                S3OriginConfig:
                  OriginAccessIdentity: ''
            DefaultCacheBehavior:
              TargetOriginId: S3Origin
              ViewerProtocolPolicy: redirect-to-https
              AllowedMethods: [GET, HEAD]
              CachedMethods: [GET, HEAD]
              ForwardedValues:
                QueryString: false
            CustomErrorResponses:
              - ErrorCode: 403
                ResponseCode: 200
                ResponsePagePath: /index.html

    Outputs:
      WebsiteURL:
        Value: !Sub "https://${{CDN.DomainName}}"
      BucketName:
        Value: !Ref SiteBucket
    """)


def _lambda_template(ctx: dict) -> str:
    handler = ctx.get("handler", "app.lambda_handler")
    runtime = ctx.get("runtime", "python3.12")
    stack = ctx.get("stack_name", "app")
    return textwrap.dedent(f"""\
    AWSTemplateFormatVersion: '2010-09-09'
    Transform: AWS::Serverless-2016-10-31
    Description: Serverless deployment — Lambda + API Gateway

    Globals:
      Function:
        Timeout: 30
        MemorySize: 256

    Resources:
      AppFunction:
        Type: AWS::Serverless::Function
        Properties:
          CodeUri: .
          Handler: {handler}
          Runtime: {runtime}
          Architectures: [x86_64]
          Role: !GetAtt AppLambdaExecutionRole.Arn
          Events:
            ApiRoot:
              Type: Api
              Properties:
                Path: /
                Method: ANY
            ApiProxy:
              Type: Api
              Properties:
                Path: /{{proxy+}}
                Method: ANY

      AppLambdaExecutionRole:
        Type: AWS::IAM::Role
        Properties:
          AssumeRolePolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Principal:
                  Service: lambda.amazonaws.com
                Action: sts:AssumeRole
          ManagedPolicyArns:
            - arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

    Outputs:
      ApiUrl:
        Description: API Gateway endpoint
        Value: !Sub "https://${{ServerlessRestApi}}.execute-api.${{AWS::Region}}.amazonaws.com/Prod/"
      FunctionArn:
        Value: !GetAtt AppFunction.Arn
    """)


def _ecs_fargate_template(ctx: dict) -> str:
    image = ctx.get("image", "IMAGE_PLACEHOLDER")
    port = ctx.get("port", 3000)
    cpu = ctx.get("cpu", 256)
    memory = ctx.get("memory", 512)
    return textwrap.dedent(f"""\
    AWSTemplateFormatVersion: '2010-09-09'
    Description: Container deployment — ECS Fargate + ALB

    Parameters:
      ContainerImage:
        Type: String
        Default: "{image}"
      VpcId:
        Type: AWS::EC2::VPC::Id
      SubnetIds:
        Type: List<AWS::EC2::Subnet::Id>

    Resources:
      ECSCluster:
        Type: AWS::ECS::Cluster
        Properties:
          ClusterName: !Sub "${{AWS::StackName}}-cluster"

      TaskDefinition:
        Type: AWS::ECS::TaskDefinition
        Properties:
          Family: !Sub "${{AWS::StackName}}-task"
          Cpu: '{cpu}'
          Memory: '{memory}'
          NetworkMode: awsvpc
          RequiresCompatibilities: [FARGATE]
          ExecutionRoleArn: !GetAtt ExecutionRole.Arn
          ContainerDefinitions:
            - Name: app
              Image: !Ref ContainerImage
              PortMappings:
                - ContainerPort: {port}
              LogConfiguration:
                LogDriver: awslogs
                Options:
                  awslogs-group: !Ref LogGroup
                  awslogs-region: !Ref AWS::Region
                  awslogs-stream-prefix: app

      LogGroup:
        Type: AWS::Logs::LogGroup
        Properties:
          LogGroupName: !Sub "/ecs/${{AWS::StackName}}"
          RetentionInDays: 14

      ExecutionRole:
        Type: AWS::IAM::Role
        Properties:
          AssumeRolePolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Principal:
                  Service: ecs-tasks.amazonaws.com
                Action: sts:AssumeRole
          ManagedPolicyArns:
            - arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

      SecurityGroup:
        Type: AWS::EC2::SecurityGroup
        Properties:
          GroupDescription: ECS service SG
          VpcId: !Ref VpcId
          SecurityGroupIngress:
            - IpProtocol: tcp
              FromPort: {port}
              ToPort: {port}
              CidrIp: 0.0.0.0/0

      Service:
        Type: AWS::ECS::Service
        Properties:
          Cluster: !Ref ECSCluster
          LaunchType: FARGATE
          DesiredCount: 1
          TaskDefinition: !Ref TaskDefinition
          NetworkConfiguration:
            AwsvpcConfiguration:
              AssignPublicIp: ENABLED
              Subnets: !Ref SubnetIds
              SecurityGroups: [!Ref SecurityGroup]

    Outputs:
      ClusterName:
        Value: !Ref ECSCluster
      ServiceName:
        Value: !GetAtt Service.Name
    """)


def _ec2_template(ctx: dict) -> str:
    instance_type = ctx.get("instance_type", "t3.micro")
    key_name = ctx.get("key_name", "my-key")
    return textwrap.dedent(f"""\
    AWSTemplateFormatVersion: '2010-09-09'
    Description: VM deployment — EC2 + Docker

    Parameters:
      InstanceType:
        Type: String
        Default: "{instance_type}"
      KeyName:
        Type: AWS::EC2::KeyPair::KeyName
        Default: "{key_name}"
      AmiId:
        Type: AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>
        Default: /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64

    Resources:
      SecurityGroup:
        Type: AWS::EC2::SecurityGroup
        Properties:
          GroupDescription: Allow HTTP and SSH
          SecurityGroupIngress:
            - IpProtocol: tcp
              FromPort: 80
              ToPort: 80
              CidrIp: 0.0.0.0/0
            - IpProtocol: tcp
              FromPort: 443
              ToPort: 443
              CidrIp: 0.0.0.0/0
            - IpProtocol: tcp
              FromPort: 22
              ToPort: 22
              CidrIp: 0.0.0.0/0

      InstanceRole:
        Type: AWS::IAM::Role
        Properties:
          AssumeRolePolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Principal:
                  Service: ec2.amazonaws.com
                Action: sts:AssumeRole
          ManagedPolicyArns:
            - arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

      InstanceProfile:
        Type: AWS::IAM::InstanceProfile
        Properties:
          Roles: [!Ref InstanceRole]

      AppInstance:
        Type: AWS::EC2::Instance
        Properties:
          InstanceType: !Ref InstanceType
          KeyName: !Ref KeyName
          ImageId: !Ref AmiId
          IamInstanceProfile: !Ref InstanceProfile
          SecurityGroupIds: [!GetAtt SecurityGroup.GroupId]
          UserData:
            Fn::Base64: !Sub |
              #!/bin/bash -xe
              yum update -y
              yum install -y docker git
              systemctl start docker
              systemctl enable docker
              usermod -aG docker ec2-user
              # Clone and run the application
              cd /home/ec2-user
              git clone ${{RepoUrl}} app
              cd app
              if [ -f Dockerfile ]; then
                docker build -t app .
                docker run -d -p 80:3000 app
              fi

      ElasticIP:
        Type: AWS::EC2::EIP
        Properties:
          InstanceId: !Ref AppInstance

    Parameters:
      RepoUrl:
        Type: String
        Description: GitHub repository URL to deploy

    Outputs:
      PublicIP:
        Value: !Ref ElasticIP
      InstanceId:
        Value: !Ref AppInstance
      WebsiteURL:
        Value: !Sub "http://${{ElasticIP}}"
    """)


# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------

_GENERATORS: dict[str, Any] = {
    "s3_cloudfront": _s3_cloudfront_template,
    "lambda": _lambda_template,
    "ecs_fargate": _ecs_fargate_template,
    "ec2": _ec2_template,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_template(
    target: str,
    context: dict | None = None,
    output_dir: str | None = None,
) -> dict:
    """Generate a CloudFormation template for the given deployment target.

    Parameters
    ----------
    target : str
        One of ``s3_cloudfront``, ``lambda``, ``ecs_fargate``, ``ec2``.
    context : dict, optional
        Variables to inject into the template (bucket_name, handler, etc.).
    output_dir : str, optional
        If provided the YAML file is also written to this directory.

    Returns
    -------
    dict
        ``{"target": ..., "template": <yaml string>, "file": <path or None>}``
    """
    gen = _GENERATORS.get(target)
    if gen is None:
        raise ValueError(f"Unsupported target: {target!r}. Choose from {list(_GENERATORS)}")

    yaml_str = gen(context or {})

    file_path = None
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        fname = f"generated-{target}.yaml"
        file_path = os.path.join(output_dir, fname)
        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write(yaml_str)

    return {
        "target": target,
        "template": yaml_str,
        "file": file_path,
    }


def list_targets() -> list[str]:
    """Return the list of supported infrastructure targets."""
    return list(_GENERATORS.keys())
