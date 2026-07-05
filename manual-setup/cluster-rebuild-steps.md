# Cluster Rebuild Cheatsheet
> Run these every session before starting work

## Pre-checks
```bash
aws sts get-caller-identity --profile ndx-admin
kubectl version --client
helm version
```

## Step 1 — NAT Gateway (console)
- VPC console → NAT Gateways → Create NAT Gateway
- Name: eks-natgw
- Subnet: eks-subnet-public-1a
- Connectivity: Public
- Elastic IP: Allocate
- Wait for Available

## Step 2 — Update Private Route Table (console)
- VPC → Route Tables → eks-rt-private
- Edit routes → 0.0.0.0/0 → new NAT Gateway
- Save

## Step 3 — EKS Cluster (console)
- Name: itp-eks-cluster
- Version: 1.33
- Role: itp-eks-cluster-role
- Subnets: all 4
- Endpoint: Public and Private
- Logs: all 5 enabled
- CloudWatch: OTel Container Insights
- Addons: VPC CNI, kube-proxy, CoreDNS, EBS CSI,
          Node monitoring, CloudWatch Observability, Metrics Server
- Wait for Active (~15 mins)

## Step 4 — Node Group (console)
- Name: itp-eks-nodegroup
- Role: itp-eks-node-role
- AMI: AL2023
- Instance: t3.medium
- Desired 2, Min 1, Max 3
- Subnets: private 1a + private 1b ONLY
- Node auto repair: enabled
- Wait for Active (~10 mins)

## Step 5 — Access Entry (console)
- EKS → itp-eks-cluster → Access tab
- Create access entry
- ARN: arn:aws:iam::440744215136:user/aadmin-dev-ndx
- Policy: AmazonEKSClusterAdminPolicy

## Step 6 — kubectl (terminal)
```bash
aws eks update-kubeconfig \
  --region us-east-1 \
  --name itp-eks-cluster \
  --profile ndx-admin

kubectl get nodes
```

## Step 7 — ALB Controller (terminal)
```bash
helm install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system \
  --set clusterName=itp-eks-cluster \
  --set serviceAccount.create=true \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"=arn:aws:iam::440744215136:role/itp-alb-contrlr-role \
  --set region=us-east-1 \
  --set vpcId=vpc-0c06396e4c158bdfc
```

## Step 8 — Verify
```bash
kubectl get nodes
kubectl get pods -n kube-system | grep aws-load-balancer
```

## End of Session Cleanup
```bash
# Delete node group first
aws eks delete-nodegroup \
  --cluster-name itp-eks-cluster \
  --nodegroup-name itp-eks-nodegroup \
  --region us-east-1 \
  --profile ndx-admin

# Wait 5 mins then delete cluster
aws eks delete-cluster \
  --name itp-eks-cluster \
  --region us-east-1 \
  --profile ndx-admin

# Get NAT Gateway ID and EIP
aws ec2 describe-nat-gateways \
  --region us-east-1 \
  --profile ndx-admin \
  --query 'NatGateways[?State==`available`].[NatGatewayId,NatGatewayAddresses[0].AllocationId]' \
  --output text

# Delete NAT and release EIP with IDs from above
aws ec2 delete-nat-gateway \
  --nat-gateway-id <id> \
  --region us-east-1 \
  --profile ndx-admin

aws ec2 release-address \
  --allocation-id <id> \
  --region us-east-1 \
  --profile ndx-admin
```

## Fixed Resource IDs (never change)
```
VPC:               vpc-0c06396e4c158bdfc
Public subnet 1a:  subnet-0f428651b4d233d37
Public subnet 1b:  subnet-0607313dbd4f8882b
Private subnet 1a: subnet-027512a4fe562cf4f
Private subnet 1b: subnet-095a1f70f06284a71
OIDC Provider:     arn:aws:iam::440744215136:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/49BA89267CE4A0F48F846A8B97587949
ALB Role:          arn:aws:iam::440744215136:role/itp-alb-contrlr-role
Node Role:         arn:aws:iam::440744215136:role/itp-eks-node-role
Cluster Role:      arn:aws:iam::440744215136:role/itp-eks-cluster-role
```