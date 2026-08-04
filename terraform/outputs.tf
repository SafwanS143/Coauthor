##############################################################################
# Outputs, including a cost estimate.
#
# Rates below were checked against AWS's own pricing pages on 2026-07-27 for
# us-east-1. They are list on-demand prices and exclude tax. AWS does change
# these; re-check before relying on the number for anything that matters.
#   EC2 on-demand ....... https://aws.amazon.com/ec2/pricing/on-demand/
#   EBS gp3 ............. https://aws.amazon.com/ebs/pricing/
#   Public IPv4 ......... https://aws.amazon.com/vpc/pricing/
##############################################################################

locals {
  # Hourly on-demand rates, us-east-1, verified 2026-07-27.
  hourly_rates = {
    "t4g.small"  = 0.0168
    "t3.small"   = 0.0208
    "t4g.medium" = 0.0336
    "t3.medium"  = 0.0416
  }

  hours_per_month = 730
  gp3_per_gb      = 0.08  # $/GB-month, includes 3000 IOPS and 125 MB/s free
  ipv4_per_hour   = 0.005 # charged whether the address is in use or idle

  instance_rate = lookup(local.hourly_rates, var.instance_type, 0.0168)
  cost_compute  = local.instance_rate * local.hours_per_month
  cost_storage  = var.volume_size_gb * local.gp3_per_gb
  cost_ipv4     = local.ipv4_per_hour * local.hours_per_month
  cost_total    = local.cost_compute + local.cost_storage + local.cost_ipv4

  dashed_ip = replace(aws_eip.coauthor.public_ip, ".", "-")
}

output "public_ip" {
  description = "Elastic IP. Stable across stop/start, which is why the certificate keeps working."
  value       = aws_eip.coauthor.public_ip
}

output "dashboard_url" {
  description = "Open this in a browser. sslip.io resolves the embedded IP, so no domain registration is needed and Let's Encrypt still issues a real certificate."
  value       = "https://coauthor.${local.dashed_ip}.sslip.io"
}

output "instance_id" {
  value = aws_instance.coauthor.id
}

output "region" {
  description = "Read back by deploy.ps1 so the AWS CLI targets the right region."
  value       = var.region
}

output "deploy_bucket" {
  description = "Upload the application tarball here; the instance pulls from it."
  value       = aws_s3_bucket.deploy.id
}

output "ssm_session_command" {
  description = "Shell on the box. No SSH key and no inbound port required."
  value       = "aws ssm start-session --target ${aws_instance.coauthor.id} --region ${var.region}"
}

output "vnc_tunnel_command" {
  description = "Port-forward for the one-time Google sign-in. Point a VNC client at localhost:5901 afterwards."
  # One line, shorthand parameters, no quotes to escape. The JSON form of
  # --parameters cannot be pasted into PowerShell: it strips the inner double
  # quotes and the AWS CLI then rejects "{portNumber:[5900]}" as invalid JSON.
  # This project is driven from PowerShell on Windows, so the output has to be
  # something that works when pasted there, and this form works in bash too.
  #
  # Local end is 5901, not 5900. The standard TightVNC installer ships a server
  # alongside the viewer and leaves it listening on 5900, so a tunnel bound
  # there is shadowed by it: the viewer reaches the local server instead and
  # reports "loopback connections are not enabled", which looks like a fault on
  # the instance when nothing ever got that far.
  value = "aws ssm start-session --target ${aws_instance.coauthor.id} --region ${var.region} --document-name AWS-StartPortForwardingSession --parameters \"portNumber=5900,localPortNumber=5901\""
}

output "estimated_monthly_cost" {
  description = "List on-demand prices, us-east-1, verified 2026-07-27. Excludes tax. Data transfer out is free for the first 100 GB/month, which this will not approach."
  value = format(
    "~$%.2f/mo = compute $%.2f (%s @ $%.4f/hr x 730) + storage $%.2f (%d GB gp3 @ $0.08) + IPv4 $%.2f ($0.005/hr x 730)",
    local.cost_total, local.cost_compute, var.instance_type, local.instance_rate,
    local.cost_storage, var.volume_size_gb, local.cost_ipv4,
  )
}
