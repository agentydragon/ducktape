load("@aspect_rules_js//js:defs.bzl", "js_test")

def seed_pair_tests(tests, size, data, entry_point, env):
    for name, args, tags in tests:
        attrs = {
            "name": name,
            "size": size,
            "args": args,
            "data": data,
            "entry_point": entry_point,
            "env": env,
        }
        if tags:
            attrs["tags"] = tags
        js_test(**attrs)
