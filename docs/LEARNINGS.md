# Debugging Journal & Key Learnings

Real errors encountered while building this platform from scratch. Every error below was hit, debugged, and solved during the build. These are not hypothetical — they are production-equivalent debugging scenarios.

---

## Error 1 — exec format error (ARM vs AMD64)

**Error:**
```
exec /usr/local/bin/python: exec format error
exec /docker-entrypoint.sh: exec format error
```

**Root cause:** Docker images built on Apple Silicon MacBook (ARM64). EKS t3.medium nodes are x86_64/AMD64. ARM binary cannot execute on AMD64 CPU.

**How I found it:** `kubectl logs <pod>` showed the error immediately on pod start.

**Fix:**
```bash
docker build --platform linux/amd64 -t <ecr-uri> .
```

**Learning:** Always specify `--platform linux/amd64` when building on M1/M2/M3 Mac for any cloud deployment. Cloud instances are almost always x86_64. This is the most common gotcha when developers switch to Apple Silicon.

**Additional issue:** Even after rebuilding, old ARM image was cached on nodes (`imagePullPolicy: IfNotPresent`). Fixed by adding `imagePullPolicy: Always` to force re-pull, then rolling restart.

---

## Error 2 — InvalidIdentityToken (OIDC Mismatch)

**Error:**
```
AssumeRoleWithWebIdentity: The web identity token provided could not be validated
InvalidIdentityToken: See the AssumeRoleWithWebIdentity documentation
```

**Root cause:** Cluster was rebuilt multiple times across sessions. Each full cluster rebuild generates a new OIDC issuer ID. The IAM OIDC provider registration and role trust policy still referenced the old cluster's OIDC ID.

```
Old cluster OIDC ID:  49BA89267CE4A0F48F846A8B97587949
New cluster OIDC ID:  1B1838B2407CFF874B9447D9438968D0
IAM trust policy:     still pointing to 49BA89267... ← mismatch
```

**How I found it:** `kubectl logs -n kube-system deployment/aws-load-balancer-controller` showed the exact STS error with the identity ARN.

**Fix:**
```bash
# Delete old OIDC provider
aws iam delete-open-id-connect-provider \
  --open-id-connect-provider-arn arn:aws:iam::440744215136:oidc-provider/...49BA89267...

# Register new one
aws iam create-open-id-connect-provider \
  --url https://oidc.eks.us-east-1.amazonaws.com/id/1B1838B2407CFF874B9447D9438968D0 \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 9e99a48a9960b14926bb7f3b02e22da2b0ab7280

# Update trust policy on IAM role to reference new OIDC ID
aws iam update-assume-role-policy --role-name itp-alb-contrlr-role ...
```

**Learning:** Three things must ALL match for IRSA to work:
1. OIDC provider registered in IAM
2. Trust policy `Federated` field on IAM role
3. JWT token issuer from current cluster

Any single mismatch = STS rejects = `AccessDenied` with zero obvious pointer to which one is wrong. Rebuilding a cluster always breaks IRSA — you must re-register and update trust policy every time.

---

## Error 3 — AccessDenied: DescribeListenerAttributes

**Error:**
```
AccessDenied: not authorized to perform: elasticloadbalancing:DescribeListenerAttributes
because no identity-based policy allows the elasticloadbalancing:DescribeListenerAttributes action
```

**Root cause:** IRSA was working (role being assumed correctly), but the IAM policy attached to `itp-alb-contrlr-role` was missing a specific permission introduced in newer versions of ALB Controller.

**How I found it:** ALB Controller logs showed the exact missing permission after IRSA was fixed.

**Fix:**
```bash
aws iam attach-role-policy \
  --role-name itp-alb-contrlr-role \
  --policy-arn arn:aws:iam::aws:policy/ElasticLoadBalancingFullAccess
```

**Learning:** When upgrading controllers or add-ons, new AWS API calls may be introduced requiring policy updates. Always use the official AWS-recommended policy document for managed components. When an IAM error appears AFTER IRSA is working — it's a missing permission in the policy, not an IRSA/OIDC issue.

---

## Error 4 — Flask Exit Code 0 (Never Started)

**Error:**
```
Last State: Terminated
Reason:     Completed
Exit Code:  0
```
Flask produced zero log output. Pod started and exited cleanly.

**Root cause:** An OpenTelemetry operator installed on the cluster was injecting init containers into pods. The OTel agent imported `app.py` as a Python module rather than executing it directly. When imported as a module, `__name__` = `'app'` not `'__main__'`. The Flask startup guard blocked execution:

```python
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)  # ← never ran
```

**How I found it:**
```bash
kubectl describe pod <pod> -n itp-app | grep "Exit Code"
# Exit Code: 0 → clean exit, not a crash
kubectl exec -it debug-backend -n itp-app -- python -c "import flask; print('ok')"
# imports worked fine → problem was startup, not dependencies
```

**Fix:** Moved `app.run()` outside the `__name__` guard:
```python
# Remove the if __name__ == '__main__' guard entirely
app.run(host='0.0.0.0', port=5000)
```

**Learning:** Exit code 0 = the process completed successfully but did nothing. When Flask produces zero logs — suspect the startup guard pattern. Production Flask apps should use Gunicorn (WSGI server) which doesn't rely on `__name__ == '__main__'` at all.

---

## Error 5 — 404 on /api/health (Path Prefix Inconsistency)

**Error:**
```
404 Not Found: The requested URL was not found on the server
curl http://<alb>/api/health → 404
```

**Root cause:** Two different path contexts in the architecture:

```
Via Nginx:  Browser → ALB → /* → Nginx → proxy_pass /api/ → Flask
            Nginx strips /api/ prefix → Flask receives /health ✓

Direct:     Browser → ALB → /api/* → Flask pod directly (target-type: ip)
            ALB does NOT strip prefix → Flask receives /api/health ✗
            Flask only had /health route → 404
```

**Fix:** Added both route decorators to each Flask endpoint:
```python
@app.route('/health')
@app.route('/api/health')
def health():
    return jsonify({"status": "healthy"})
```

**Learning:** Path routing strategy must be consistent across all layers — ALB Listener Rules, Nginx `proxy_pass`, and application routes. When using `target-type: ip`, ALB routes directly to pods without path rewriting. The full path including prefix reaches the application.

---

## Error 6 — MySQL Pending: EBS AZ Lock + Node Full

**Error:**
```
0/2 nodes are available:
  1 Too many pods
  1 node(s) didn't match PersistentVolume's node affinity
```

**Root cause:** Two separate problems:
1. EBS volume was created in `us-east-1b`. After scaling nodes back up, one node landed in `us-east-1a` — EBS volumes are AZ-locked, cannot attach across AZs.
2. The `us-east-1b` node had exactly 17 pods running — t3.medium VPC CNI limit.

**How I found it:**
```bash
kubectl describe pod mysql-0 -n itp-app | grep -A5 "Events"
kubectl get pv <pv-name> -o jsonpath='{.spec.nodeAffinity}'
kubectl get nodes -o custom-columns="NAME:.metadata.name,AZ:.metadata.labels.topology\.kubernetes\.io/zone"
kubectl get pods -A -o wide | grep <node-name>  # count pods per node
```

**Fix:** Scaled node group to 3 nodes (`desiredSize=3`). Third node landed in `us-east-1b` with capacity — MySQL scheduled immediately.

**Learning:** EBS volumes are AZ-locked — a fundamental constraint of EBS. StatefulSets with EBS PVCs cannot freely reschedule across AZs. This is the primary reason production databases use RDS Multi-AZ instead of MySQL StatefulSet — RDS handles AZ failover automatically.

---

## Error 7 — GitHub Actions: eks:DescribeCluster AccessDenied

**Error:**
```
AccessDeniedException: User: arn:aws:sts::440744215136:assumed-role/itp-github-actions-role
is not authorized to perform: eks:DescribeCluster
```

**Root cause:** `AmazonEKSClusterPolicy` does not include `eks:DescribeCluster`. This permission is needed by `aws eks update-kubeconfig` to download the cluster's kubeconfig.

**Fix:**
```bash
aws iam put-role-policy \
  --role-name itp-github-actions-role \
  --policy-name eks-describe-cluster \
  --policy-document '{"Statement":[{"Effect":"Allow","Action":["eks:DescribeCluster","eks:ListClusters"],"Resource":"*"}]}'
```

---

## Error 8 — kubectl: server asked for credentials

**Error:**
```
error validating data: failed to download openapi:
the server has asked for the client to provide credentials
```

**Root cause:** Two separate authorization layers for EKS kubectl access:
- **Layer 1 (IAM):** Can call `eks:DescribeCluster` AWS API — gets kubeconfig ✓
- **Layer 2 (EKS access entry):** Can run kubectl commands inside the cluster ✗

The GitHub Actions role had Layer 1 but not Layer 2. EKS cluster has its own internal RBAC that must be configured separately from IAM.

**Fix:** Create EKS access entry and associate cluster admin policy:
```bash
aws eks create-access-entry \
  --cluster-name itp-eks-cluster \
  --principal-arn arn:aws:iam::440744215136:role/itp-github-actions-role \
  --type STANDARD

aws eks associate-access-policy \
  --cluster-name itp-eks-cluster \
  --principal-arn arn:aws:iam::440744215136:role/itp-github-actions-role \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
  --access-scope type=cluster
```

**Learning:** EKS has TWO separate authorization systems:
1. **IAM** — controls AWS API calls (DescribeCluster, CreateNodegroup)
2. **EKS access entries** — controls Kubernetes API calls (kubectl get pods, kubectl apply)

Any new identity that needs kubectl access requires BOTH. IAM gets the kubeconfig. Access entry grants cluster permissions.

---

## Framework for Debugging AWS Permission Errors

Every `AccessDenied` in AWS tells you exactly three things:

```
WHO is trying:    arn:aws:sts::440744215136:assumed-role/<role-name>
WHAT they want:   <service>:<Action>
ON WHAT:          arn:aws:<service>:...:resource
```

Steps to debug:
1. Read the error — WHO, WHAT, ON WHAT
2. Ask: is this an IAM policy issue or a resource-level auth issue?
3. IAM policy = attach/create policy on the role
4. Resource-level = configure auth on the resource itself (EKS access entry, S3 bucket policy, ECR repo policy)
5. Re-run and check the next permission error

Permission errors come in layers. Fix one, find the next. This is normal — not a sign something is broken.

---

## Key Architectural Decisions

**Why MySQL StatefulSet instead of RDS?**
RDS `db.t3.micro` was unavailable in this AWS account. MySQL StatefulSet with EBS PVC demonstrates the same Kubernetes patterns (StatefulSet, PVC, Headless Service) while keeping costs at zero. The Flask connection string is environment-variable driven — switching to RDS is a one-line change in the Kubernetes Secret.

**Why `target-type: ip` instead of `instance`?**
IP mode sends traffic directly from ALB to pod IPs via VPC CNI — bypasses kube-proxy, one less network hop, lower latency. Requires VPC CNI (standard on EKS). Instance mode goes node-port → kube-proxy → pod, adding unnecessary hops.

**Why Headless Service for MySQL?**
Headless Service (`clusterIP: None`) enables stable per-pod DNS (`mysql-0.mysql-svc.itp-app.svc.cluster.local`). If scaling to multiple MySQL replicas, the application can target `mysql-0` specifically for writes. Standard pattern for all StatefulSet workloads.

**Why paths filter in GitHub Actions?**
```yaml
paths:
  - 'app/**'
  - 'k8s-manifests/**'
```
Without this filter, every README update, diagram change, or documentation commit triggers a full Docker build and EKS deployment — wasteful and noisy. Path filters ensure the pipeline only runs when application or infrastructure code actually changes.
