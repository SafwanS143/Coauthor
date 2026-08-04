variable "region" {
  description = "AWS region. us-east-1 is the cheapest; ca-central-1 keeps the data in Canada."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = <<-EOT
    Must have 2 GB of RAM. Chromium with a Google Docs editor open sits at
    600-900 MB resident, so anything .micro gets OOM-killed, typically overnight.
    t4g.small is arm64/Graviton; use t3.small for x86 at a higher price.
  EOT
  type        = string
  default     = "t4g.small"

  validation {
    condition     = can(regex("small|medium|large", var.instance_type))
    error_message = "Use at least a .small -- Chromium will be OOM-killed on 1 GB."
  }
}

variable "volume_size_gb" {
  description = "Root volume. 12 GB fits Ubuntu, Chromium, the browser profile and years of SQLite."
  type        = number
  default     = 12
}

variable "timezone" {
  description = "System timezone on the instance. Only affects log timestamps; the dashboard renders Toronto time regardless."
  type        = string
  default     = "America/Toronto"
}
