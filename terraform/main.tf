##############################################################################
# coauthor -- EC2 host for the presence watcher, activity poller and dashboard
#
# Deliberate choices:
#   * No SSH. Port 22 is closed and no key pair exists. Administration is via
#     SSM Session Manager, which needs no inbound rule at all.
#   * Only 80 and 443 are open. 80 exists solely so Caddy can answer the
#     Let's Encrypt HTTP-01 challenge and redirect everything to HTTPS.
#   * The root volume is encrypted. It holds chrome-profile/, which contains
#     live Google session cookies -- that is the whole reason encryption is on.
#   * An Elastic IP, so the hostname baked into the TLS certificate survives a
#     stop/start. A public IPv4 costs the same either way.
#   * IMDSv2 required, so a request-forgery bug in anything on the box cannot
#     trivially read instance credentials.
##############################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = "coauthor"
      ManagedBy = "terraform"
    }
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Canonical publishes the current Ubuntu AMI id as a public SSM parameter,
# which is steadier than filtering AMI names by wildcard.
data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

resource "random_id" "suffix" {
  byte_length = 4
}

##############################################################################
# Deployment bucket -- how application code reaches the box without SSH
##############################################################################

resource "aws_s3_bucket" "deploy" {
  bucket        = "coauthor-deploy-${random_id.suffix.hex}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "deploy" {
  bucket                  = aws_s3_bucket.deploy.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "deploy" {
  bucket = aws_s3_bucket.deploy.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "deploy" {
  bucket = aws_s3_bucket.deploy.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Versioning means a deleted object is only hidden behind a delete marker, and
# secrets.tar.gz carries live Google session cookies. Without this rule those
# versions would sit in the bucket forever after the deploy "removed" them.
resource "aws_s3_bucket_lifecycle_configuration" "deploy" {
  bucket     = aws_s3_bucket.deploy.id
  depends_on = [aws_s3_bucket_versioning.deploy]

  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    expiration {
      expired_object_delete_marker = true
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

##############################################################################
# IAM -- SSM access plus read-only on the deployment bucket
##############################################################################

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "coauthor-instance-${random_id.suffix.hex}"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "deploy_read" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.deploy.arn}/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.deploy.arn]
  }
  # The instance deletes the credentials bundle as soon as it has unpacked it,
  # so that live Google session cookies do not linger in the bucket. Scoped to
  # that one key -- nothing else on the box has any reason to delete objects.
  statement {
    actions   = ["s3:DeleteObject"]
    resources = ["${aws_s3_bucket.deploy.arn}/secrets.tar.gz"]
  }
}

resource "aws_iam_role_policy" "deploy_read" {
  name   = "coauthor-deploy-read"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.deploy_read.json
}

resource "aws_iam_instance_profile" "instance" {
  name = "coauthor-instance-${random_id.suffix.hex}"
  role = aws_iam_role.instance.name
}

##############################################################################
# Network
##############################################################################

resource "aws_security_group" "web" {
  name        = "coauthor-web-${random_id.suffix.hex}"
  description = "coauthor: HTTP/HTTPS in, everything out. No SSH by design."
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTP - ACME challenge and redirect to HTTPS"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS - the dashboard, behind the shared password"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Outbound to Google, Lets Encrypt, apt and SSM"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

##############################################################################
# Instance
#
# t4g.small: 2 vCPU / 2 GB. A .micro is not enough -- Chromium with a Docs
# editor open sits around 600-900 MB resident and gets OOM-killed on 1 GB.
##############################################################################

resource "aws_instance" "coauthor" {
  ami                    = data.aws_ssm_parameter.ubuntu.value
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.web.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  # associate_public_ip_address is deliberately not set. The default subnet
  # auto-assigns one, and attaching the Elastic IP below releases that address
  # and takes its place -- so there is exactly one public IPv4 charge either
  # way. Pinning it to false here reads back as true once the EIP is attached,
  # which Terraform treats as drift and "fixes" by rebuilding the instance on
  # every single apply.

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.volume_size_gb
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
    http_endpoint               = "enabled"
  }

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/user_data.sh", {
    deploy_bucket = aws_s3_bucket.deploy.id
    timezone      = var.timezone
  })

  tags = { Name = "coauthor" }

  lifecycle {
    # The AMI parameter moves as Canonical publishes updates; that alone should
    # not destroy and rebuild a box holding a logged-in browser profile.
    #
    # user_data is ignored for the same reason, and a stronger one: it only ever
    # executes on first boot, so editing the bootstrap script changes nothing
    # about a running instance -- yet Terraform treats it as forcing
    # replacement, which would silently destroy the database, the signed-in
    # profile and the TLS certificate. Fixing a comment in that script must not
    # be able to wipe the box. A rebuilt instance still gets the current
    # version, because this only suppresses the diff on an existing one.
    ignore_changes = [ami, user_data]
  }
}

resource "aws_eip" "coauthor" {
  domain = "vpc"
  tags   = { Name = "coauthor" }
}

resource "aws_eip_association" "coauthor" {
  instance_id   = aws_instance.coauthor.id
  allocation_id = aws_eip.coauthor.id
}
