use crate::{
    Fixture, LogicalModuleEntry, Member, assert_module_exports, assert_preserved_entry_output,
    logical_module,
};

pub fn unescaped_local_callable_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module(
            "values/dynamic",
            &[Member::new("dynamic"), Member::new("prefix")],
        ),
        logical_module(
            "values/later",
            &[Member::new("later"), Member::new("readLater")],
        ),
    ]
}

pub fn assert_unescaped_local_callable_split(fixture: &Fixture) {
    assert_preserved_entry_output(fixture, "1\n");
    for (path, exports) in [
        ("static/app/modules/values/dynamic.js", &["dynamic"]),
        ("static/app/modules/values/later.js", &["later"]),
    ] {
        assert_module_exports(&fixture.out_root, path, exports, &[]);
    }
}
