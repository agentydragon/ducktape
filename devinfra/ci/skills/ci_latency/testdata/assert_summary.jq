.occupancy.completed_job_peak==20 and
([.longest_queues[] | select(.html_url|endswith("102641166641"))][0] |
  .queue_seconds==298 and .runtime_seconds==89) and
([.longest_queues[] | select(.html_url|endswith("102645478845"))][0] |
  .queue_seconds==525 and .runtime_seconds==null)
