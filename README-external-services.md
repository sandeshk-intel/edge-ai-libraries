# External Services Guide — AWS S3 and AWS Managed OpenSearch

This guide explains how to configure the **Video Search and Summarization (VSS)** Helm chart to use **AWS S3** (instead of the built-in MinIO server) and **AWS Managed OpenSearch** (instead of the built-in VDMS Vector DB).

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [AWS S3 Configuration](#2-aws-s3-configuration)
   - [Prerequisites](#21-prerequisites)
   - [IAM Policy](#22-iam-policy)
   - [Helm Values](#23-helm-values)
   - [Per-Subchart S3 Endpoint Override](#24-per-subchart-s3-endpoint-override)
   - [Connectivity Checklist](#25-s3-connectivity-checklist)
3. [AWS Managed OpenSearch Configuration](#3-aws-managed-opensearch-configuration)
   - [Prerequisites](#31-prerequisites)
   - [Security Group Rules](#32-security-group-rules)
   - [Authentication Modes](#33-authentication-modes)
   - [Helm Values](#34-helm-values)
   - [Connectivity Checklist](#35-opensearch-connectivity-checklist)
4. [Combined S3 + OpenSearch Deployment](#4-combined-s3--opensearch-deployment)
5. [Override Files Reference](#5-override-files-reference)
6. [Troubleshooting](#6-troubleshooting)

---

## 1. Architecture Overview

By default the chart deploys in-cluster backing services:

| Default Service   | Managed Replacement     | Enable switch                         |
|-------------------|-------------------------|---------------------------------------|
| MinIO (in-cluster)| AWS S3                  | `minioserver.enabled: false`          |
| VDMS Vector DB    | AWS Managed OpenSearch  | `global.vectorDbType: "opensearch"`   |

When either external service is used, the relevant in-cluster subchart is disabled and the pods are pointed at the managed endpoint via environment variables.

---

## 2. AWS S3 Configuration

### 2.1 Prerequisites

Before deploying with S3 the following infrastructure must be in place:

1. **S3 Bucket** — Create a bucket in the same AWS region as your EKS cluster.
   ```
   aws s3api create-bucket \
     --bucket <your-bucket-name> \
     --region us-west-2 \
     --create-bucket-configuration LocationConstraint=us-west-2
   ```

2. **Block Public Access** — Keep all public-access blocks enabled on the bucket (the application authenticates with credentials, not public URLs).

3. **IAM User or IAM Role with bucket permissions** — See [IAM Policy](#22-iam-policy) below.

4. **VPC Endpoint for S3** — **Required** for EKS clusters in a private VPC to avoid traffic routing over the public internet and to eliminate NAT Gateway data transfer charges.
   ```
   aws ec2 create-vpc-endpoint \
     --vpc-id <vpc-id> \
     --service-name com.amazonaws.us-west-2.s3 \
     --route-table-ids <route-table-id>
   ```
   After creation, add the S3 endpoint hostname (`s3.us-west-2.amazonaws.com`) to `global.proxy.no_proxy` to prevent the application proxy from intercepting S3 requests:
   ```yaml
   global:
     proxy:
       no_proxy: "localhost,127.0.0.1,...,s3.us-west-2.amazonaws.com,.s3.amazonaws.com"
   ```

5. **Bucket CORS policy** — Required if the UI fetches media assets directly from S3 (via the nginx `/minio/` proxy). Add the following to your bucket's CORS configuration:
   ```json
   [
     {
       "AllowedHeaders": ["*"],
       "AllowedMethods": ["GET", "HEAD"],
       "AllowedOrigins": ["*"],
       "ExposeHeaders": ["ETag"]
     }
   ]
   ```

6. **Security Groups** — Ensure EKS node security groups allow **outbound HTTPS (TCP 443)** to `s3.us-west-2.amazonaws.com` (or use the VPC endpoint, which bypasses security group rules).

### 2.2 IAM Policy

Attach the following least-privilege policy to the IAM user (access key) or instance role used by the pods:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "VSSBucketAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": [
        "arn:aws:s3:::<your-bucket-name>",
        "arn:aws:s3:::<your-bucket-name>/*"
      ]
    }
  ]
}
```

> **Note:** If you use IAM Roles for Service Accounts (IRSA), set `AWS_REGION` and annotate the Kubernetes service account instead of providing static credentials. Static credentials (`MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`) map to `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` inside the pipeline-manager pod.

### 2.3 Helm Values

In `user_values_override.yaml` (or equivalent secrets file — **do not commit credentials**):

```yaml
# Disable in-cluster MinIO
minioserver:
  enabled: false

global:
  env:
    # AWS S3 endpoint for the region where your bucket lives
    MINIO_HOST: "s3.us-west-2.amazonaws.com"
    MINIO_PORT: "443"
    # IAM credentials (Access Key ID / Secret Access Key)
    MINIO_ROOT_USER: "<AWS_ACCESS_KEY_ID>"
    MINIO_ROOT_PASSWORD: "<AWS_SECRET_ACCESS_KEY>"
    # AWS region — must match the bucket's region
    AWS_REGION: "us-west-2"
    # VPC ID used for resolving VPC endpoint requests (optional but recommended)
    AWS_VPC_ID: "<your-vpc-id>"
    # Bucket name (used by the pipeline-manager as MINIO_BUCKET)
    DEFAULT_BUCKET_NAME: "<your-bucket-name>"

  proxy:
    # Add S3 hostname to no_proxy so the application does not route S3 calls through HTTP proxy
    no_proxy: "localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.svc.cluster.local,s3.us-west-2.amazonaws.com,.s3.amazonaws.com"

videoSummaryManager:
  # Use HTTPS for AWS S3
  minioProtocol: "https:"
  minioBucket: "<your-bucket-name>"
```

### 2.4 Per-Subchart S3 Endpoint Override

The following subcharts have their own `minioServer.name` / `minioServer.service.port` values that must also point to S3 when the in-cluster MinIO is disabled:

| Subchart           | Override key                                 |
|--------------------|----------------------------------------------|
| `audioanalyzer`    | `audioanalyzer.minioServer.name`             |
|                    | `audioanalyzer.minioServer.service.port`     |
| `videoingestion`   | `videoingestion.minioServer.name`            |
|                    | `videoingestion.minioServer.service.port`    |
| `vdmsdataprep`     | `vdmsdataprep.minioServer.name`              |
|                    | `vdmsdataprep.minioServer.service.port`      |
|                    | `vdmsdataprep.minioServer.secure`            |

Add the following to your override file when using any of these subcharts with AWS S3:

```yaml
audioanalyzer:
  minioServer:
    name: "s3.us-west-2.amazonaws.com"
    service:
      port: 443   # HTTPS port for AWS S3

videoingestion:
  minioServer:
    name: "s3.us-west-2.amazonaws.com"
    service:
      port: 443

vdmsdataprep:
  minioServer:
    name: "s3.us-west-2.amazonaws.com"
    service:
      port: 443
    secure: "true"   # Enables MINIO_SECURE=true in the dataprep configmap
```

### 2.5 S3 Connectivity Checklist

Run these checks from a pod in the same namespace before deploying the full stack:

```bash
# 1. Verify DNS resolution of S3 endpoint from inside the cluster
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  nslookup s3.us-west-2.amazonaws.com

# 2. Verify HTTPS connectivity to S3 (expect HTTP 200 or 403, not a timeout)
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl -v --max-time 10 https://s3.us-west-2.amazonaws.com

# 3. Verify bucket access with credentials
kubectl run -it --rm debug --image=amazon/aws-cli --restart=Never -- \
  aws s3 ls s3://<your-bucket-name> \
  --region us-west-2 \
  --endpoint-url https://s3.us-west-2.amazonaws.com

# 4. Check no_proxy is propagated correctly in the pipeline-manager pod
kubectl exec deploy/videosummarybackend -- env | grep -i proxy
```

**Common connectivity issues:**

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Connection timed out` to S3 | Missing VPC endpoint or SG outbound rule | Create Gateway VPC endpoint for S3; allow TCP 443 outbound |
| `Access Denied` on bucket ops | Insufficient IAM policy | Attach policy from [section 2.2](#22-iam-policy) |
| `SSL handshake error` | Proxy intercepting S3 traffic | Add S3 hostname to `no_proxy` |
| `SignatureDoesNotMatch` | Wrong `AWS_REGION` set | Ensure region matches the bucket's region |
| `NoSuchBucket` | Bucket does not exist or wrong region | Verify bucket name and `AWS_REGION` value |
| Nginx `/minio/` returns 502 | `MINIO_HOST` not set or wrong port | Set `global.env.MINIO_HOST` and port; set `minioProtocol: "https:"` |

---

## 3. AWS Managed OpenSearch Configuration

### 3.1 Prerequisites

1. **OpenSearch Domain** — Create a VPC-based OpenSearch Service domain in the same VPC as your EKS cluster for private, low-latency access.
   - Recommended: enable **fine-grained access control** with a master user.
   - Recommended: enable **encryption at rest** and **node-to-node encryption**.
   - Recommended: enable **HTTPS-only** policy on the domain.

2. **VPC Placement** — Deploy the OpenSearch domain in the **same VPC and subnets** as the EKS node groups. Cross-VPC access requires VPC peering and additional DNS resolution steps.

3. **Security Group on OpenSearch domain** — Create or update the security group attached to the OpenSearch domain:
   - Allow **inbound TCP 443** from the EKS node group security group (or pod CIDR if using CNI network policy).

4. **Security Group on EKS nodes** — Allow **outbound TCP 443** to the OpenSearch domain security group.

5. **DNS resolution** — The OpenSearch VPC endpoint (e.g. `vpc-<name>.us-west-2.es.amazonaws.com`) must resolve from inside the cluster. Verify with `nslookup` from a test pod before deploying.

6. **Index pre-creation (recommended)** — Although the application will auto-create the index on first ingest, pre-creating it with the correct mapping avoids schema conflicts:
   ```bash
   curl -u "<user>:<password>" \
     -X PUT "https://<opensearch-endpoint>/video_frame_embeddings" \
     -H "Content-Type: application/json" \
     -d '{"settings": {"number_of_shards": 1, "number_of_replicas": 1}}'
   ```

### 3.2 Security Group Rules

| Direction | Protocol | Port | Source/Dest          | Purpose                          |
|-----------|----------|------|----------------------|----------------------------------|
| Inbound   | TCP      | 443  | EKS node SG          | HTTPS from pods to OpenSearch    |
| Outbound  | TCP      | 443  | OpenSearch domain SG | Pods connecting to OpenSearch    |

> For pod-level isolation with a CNI that supports network policy (Calico, Cilium), also restrict at the pod level to only the `video-search` and `vdms-dataprep` pods.

### 3.3 Authentication Modes

The chart supports two authentication modes for OpenSearch:

**Mode A — Basic Auth (username/password):**  
Set `global.opensearch.awsRegion: ""` and provide `user` / `password`. Requires fine-grained access control to be enabled on the domain.

**Mode B — AWS IAM Auth (SigV4 signing):**  
Set `global.opensearch.awsRegion: "us-west-2"`. The pods will sign requests with the node's instance role or IRSA service account credentials. Leave `user` and `password` empty.

### 3.4 Helm Values

```yaml
global:
  # Switch vector database backend to OpenSearch
  vectorDbType: "opensearch"

  opensearch:
    # VPC endpoint hostname of the managed domain (no https:// prefix)
    host: "vpc-<domain-name>.<region>.es.amazonaws.com"
    port: 443
    # Index name (leave empty to inherit global.vdmsIndexName)
    index: "video_frame_embeddings"

    # --- Basic Auth (Mode A) ---
    user: "<master-username>"
    password: "<master-password>"
    useSSL: true
    verifyCerts: false    # Set to true and supply caCert if you manage your own CA

    # --- IAM Auth (Mode B) — mutually exclusive with basic auth above ---
    # awsRegion: "us-west-2"   # Uncomment to switch to SigV4; leave user/password empty

# Disable VDMS Vector DB — not used when OpenSearch is the backend
vdmsvectordb:
  enabled: false

# Enable search components and point them at OpenSearch-compatible images
videosearch:
  enabled: true
  videosearch:
    image:
      repository: <your-ecr-or-registry>/vss
      tag: "video-search-opensearch"
      pullPolicy: Always

vdmsdataprep:
  enabled: true
  image:
    repository: <your-ecr-or-registry>/vss
    tag: "vdms-dataprep-opensearch"
    pullPolicy: Always

global:
  proxy:
    # Add OpenSearch endpoint to no_proxy so TLS is not broken by proxy
    no_proxy: "localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.svc.cluster.local,vpc-<domain-name>.<region>.es.amazonaws.com"
```

### 3.5 OpenSearch Connectivity Checklist

```bash
# 1. Verify DNS resolution of OpenSearch VPC endpoint from within the cluster
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  nslookup vpc-<domain-name>.us-west-2.es.amazonaws.com

# 2. Verify HTTPS connectivity — expect 200 or 401, not a timeout
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl -v --max-time 10 -k \
  https://vpc-<domain-name>.us-west-2.es.amazonaws.com

# 3. Verify authenticated access (basic auth)
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl -u "<user>:<password>" -k \
  "https://vpc-<domain-name>.us-west-2.es.amazonaws.com/_cluster/health?pretty"

# 4. Check OpenSearch env vars are set correctly in video-search pod
kubectl exec deploy/videosearch -- env | grep -i opensearch
```

**Common connectivity issues:**

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Connection timed out` on port 443 | OpenSearch SG missing inbound rule | Allow TCP 443 inbound from EKS node SG |
| `NXDOMAIN` for VPC endpoint | Domain not in same VPC | Create domain in same VPC; or set up Route 53 Private Hosted Zone for cross-VPC |
| `AuthenticationException` | Wrong credentials or fine-grained access disabled | Enable fine-grained access control; verify user/pass |
| `SSL: CERTIFICATE_VERIFY_FAILED` | `verifyCerts: true` but no CA provided | Set `verifyCerts: false` for AWS-managed domains, or supply the AWS root CA |
| Proxy interfering with TLS | HTTP proxy re-encrypting traffic | Add OpenSearch hostname to `no_proxy` |
| `404 index_not_found_exception` | Index not yet created | Trigger a first ingest or pre-create index manually |
| `403 Forbidden` | IAM policy or domain access policy blocking request | Review domain access policy; ensure node/pod role is in allowlist |

---

## 4. Combined S3 + OpenSearch Deployment

To deploy with both AWS S3 **and** AWS Managed OpenSearch, layer the following override files:

```bash
helm upgrade --install vss . \
  -f unified_summary_search.yaml \
  -f opensearch_override.yaml \
  -f user_values_override.yaml
```

Where `opensearch_override.yaml` includes:
- `global.vectorDbType: "opensearch"`
- `global.opensearch.*` values
- `vdmsvectordb.enabled: false`
- OpenSearch-specific image tags for `videosearch` and `vdmsdataprep`
- `vdmsdataprep.minioServer.*` pointing at S3

And `user_values_override.yaml` provides all secrets (`MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `POSTGRES_USER`, credentials, etc.).

> **Security reminder:** Never commit `user_values_override.yaml` with real credentials to version control. Use a secrets manager (AWS Secrets Manager, HashiCorp Vault, Kubernetes Secrets with SOPS/Sealed Secrets) for production deployments.

---

## 5. Override Files Reference

| File | Purpose |
|------|---------|
| `values.yaml` | Default chart values — committed to repo |
| `summary_override.yaml` | Enable summary-only pipeline (VLM + AudioAnalyzer + VideoIngestion) |
| `search_override.yaml` | Enable search-only pipeline (VDMS + MultimodalEmbeddingMS + VideoSearch) |
| `unified_summary_search.yaml` | Enable both summary and search pipelines together |
| `opensearch_override.yaml` | Switch vector DB from VDMS to AWS Managed OpenSearch |
| `vllm_override.yaml` | Use an external vLLM service instead of in-cluster VLM Inference |
| `ovms_override.yaml` | Use OVMS for LLM inference |
| `user_values_override.yaml` | **User-specific secrets and overrides — DO NOT COMMIT** |

---

## 6. Troubleshooting

### General debugging steps

```bash
# Check all pod statuses
kubectl get pods -n <namespace>

# Describe a pod to see init container failures or image pull errors
kubectl describe pod <pod-name> -n <namespace>

# Follow logs for the pipeline-manager (main orchestrator)
kubectl logs -f deploy/videosummarybackend -n <namespace>

# Follow logs for video-search
kubectl logs -f deploy/videosearch -n <namespace>

# Follow logs for vdms-dataprep
kubectl logs -f deploy/vdms-dataprep -n <namespace>

# Verify all environment variables are correctly set
kubectl exec deploy/videosummarybackend -n <namespace> -- env | sort
```

### Verifying S3 bucket connectivity from the pipeline-manager pod

```bash
kubectl exec -it deploy/videosummarybackend -n <namespace> -- \
  curl -v --max-time 10 \
  -H "Host: <your-bucket-name>.s3.us-west-2.amazonaws.com" \
  "https://s3.us-west-2.amazonaws.com/<your-bucket-name>/"
```

### Verifying OpenSearch connectivity from the videosearch pod

```bash
kubectl exec -it deploy/videosearch -n <namespace> -- \
  curl -sk -u "<user>:<password>" \
  "https://<opensearch-host>/_cluster/health?pretty"
```
