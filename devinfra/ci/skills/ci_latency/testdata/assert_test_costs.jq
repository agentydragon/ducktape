length == 1
and .[0].label == "//agentplane/app:test_thread_browser"
and .[0].fresh_actions == 2
and ((.[0].process_seconds - 232.582083) | fabs) < 0.00001
and ((.[0].longest_action_seconds - 122.470950) | fabs) < 0.00001
and ((.[0].cpu_seconds - 472.764167) | fabs) < 0.00001
and .[0].missing_cpu_actions == 0
