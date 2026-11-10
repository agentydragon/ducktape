# Extract one closed PID 1 function-graph ancestry, not independent symbol counts.
# The caller bounds the input scan and passes tracefs overrun/drop integrity.
BEGIN {
    if (integrity == "")
        integrity = "clean"
    roots = 0
    roots_other_pid = 0
}
function function_name(body, name) {
    name = body
    sub(/\(.*/, "", name)
    return name
}

function bounded(raw) {
    if (length(raw) > 256)
        return substr(raw, 1, 256) " [line_truncated]"
    return raw
}

integrity != "clean" { next }
/^#/ { next }
! /\|/ { next }

{
    raw = $0
    body = raw
    sub(/^.*\|[[:space:]]*/, "", body)

    if (body ~ /^cgroup_subtree_control_write\(\)[[:space:]]*\{$/) {
        if (writer_pid != "" && raw !~ ("-" writer_pid "[[:space:]]")) {
            roots_other_pid++
            next
        }
        roots++
        depth = 1
        stack[1] = "cgroup_subtree_control_write"
        stack_line[1] = NR
        stack_raw[1] = raw
        candidate = 0
        next
    }
    if (depth == 0)
        next

    if (body ~ /^\}/) {
        delete stack[depth]
        delete stack_line[depth]
        delete stack_raw[depth]
        depth--
        if (depth == 0 && candidate) {
            print "status=proven tree=" roots " root_start=" root_start " root_end=" NR " update_line=" update_line " lock_line=" lock_line " rcu_line=" rcu_line " path=" path
            print "context=root line=" root_start " raw=" bounded(root_raw)
            print "context=update line=" update_line " raw=" bounded(update_raw)
            print "context=lock line=" lock_line " raw=" bounded(lock_raw)
            print "context=rcu line=" rcu_line " raw=" bounded(rcu_raw)
            selected = 1
            exit
        }
        next
    }

    name = function_name(body)
    opens = body ~ /\{$/
    if (opens) {
        depth++
        if (depth > 64) {
            depth = 0
            too_deep = 1
            next
        }
        stack[depth] = name
        stack_line[depth] = NR
        stack_raw[depth] = raw
    }
    if (name !~ /^rcu_sync_enter([.]|$)/)
        next

    update_index = 0
    lock_index = 0
    for (i = 2; i <= depth; i++) {
        if (!update_index && stack[i] ~ /^cgroup_update_dfl_csses([.]|$)/)
            update_index = i
        if (update_index && !lock_index && stack[i] ~ /^percpu_down_write([.]|$)/)
            lock_index = i
    }
    if (!update_index || !lock_index || lock_index <= update_index)
        next

    candidate = 1
    root_start = stack_line[1]
    root_raw = stack_raw[1]
    update_line = stack_line[update_index]
    update_raw = stack_raw[update_index]
    lock_line = stack_line[lock_index]
    lock_raw = stack_raw[lock_index]
    rcu_line = NR
    rcu_raw = raw
    path = stack[1]
    for (i = 2; i <= depth; i++)
        path = path ">" stack[i]
    if (!opens)
        path = path ">" name
    if (length(path) > 900) {
        candidate = 0
        path_too_long = 1
    }
}

END {
    if (integrity != "clean")
        print "status=unproven reason=trace_integrity_" integrity
    else if (!selected)
        print "status=unproven roots=" roots " incomplete_root=" (depth > 0 ? "yes" : "no") " too_deep=" (too_deep ? "yes" : "no") " path_too_long=" (path_too_long ? "yes" : "no") " reason=no_closed_nested_path"
}
