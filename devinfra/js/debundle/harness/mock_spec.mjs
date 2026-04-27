export function createMockBrowserBundleTransformSpec({
  appRoot,
  assetSummaryPath,
  jsListPath,
  snapshotRoot,
  transformedRoot,
}) {
  return {
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "rename_profile_id",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "e$3",
          },
        },
        target: {
          name: "profileId",
        },
        fingerprint: {
          initEquals: "\"profile-7\"",
        },
      },
      {
        id: "rename_profile_name",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "t",
          },
        },
        target: {
          name: "profileName",
        },
        fingerprint: {
          initEquals: "\"Ada Lovelace\"",
        },
      },
      {
        id: "rename_profile_tags",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "a",
          },
        },
        target: {
          name: "profileTags",
        },
        fingerprint: {
          initStartsWith: "[\"analysis\", \"dom\"]",
        },
      },
      {
        id: "rename_profile_loader",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "o$1",
          },
        },
        target: {
          name: "loadProfileRecord",
        },
        fingerprint: {
          initStartsWith: "() => ({ id: e$3, name: t, tags: [...a] })",
        },
      },
      {
        id: "rename_sum_numbers",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "e$2",
          },
        },
        target: {
          name: "sumNumbersImpl",
        },
        fingerprint: {
          initStartsWith: "(t2, a2) => t2 + a2",
        },
      },
      {
        id: "rename_join_tags",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "o",
          },
        },
        target: {
          name: "joinTagList",
        },
        fingerprint: {
          initStartsWith: "t2 => t2.join(\"|\")",
        },
      },
      {
        id: "rename_count_unique_items",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "n",
          },
        },
        target: {
          name: "countUniqueItems",
        },
        fingerprint: {
          initStartsWith: "t2 => Array.from(new Set(t2)).length",
        },
      },
      {
        id: "rename_runtime_info",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "e$1",
          },
        },
        target: {
          name: "bundleRuntimeInfo",
        },
        fingerprint: {
          initEquals: "{ stamp: \"mock-dashboard@7\" }",
        },
      },
      {
        id: "rename_compute_total",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "e",
          },
        },
        target: {
          name: "computeDashboardTotal",
        },
        fingerprint: {
          initStartsWith: "a2 => e$2(a2.id.length, n(a2.tags))",
        },
      },
      {
        id: "rename_model_class",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "ClassDeclaration",
            name: "A",
          },
        },
        target: {
          name: "MockDashboardModel",
        },
        fingerprint: {
          memberNamesPrefix: ["constructor", "snapshot"],
          superClass: null,
        },
      },
      {
        id: "rename_model_builder",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "s",
          },
        },
        target: {
          name: "buildDashboardModel",
        },
        fingerprint: {
          initStartsWith: "a2 => new A(a2, e(a2))",
        },
      },
      {
        id: "rename_summary_builder",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "d",
          },
        },
        target: {
          name: "buildSummaryCard",
        },
        fingerprint: {
          initStartsWith: "a2 => ({ headline: `${a2.profile.name}:${a2.total}`, total: a2.total, tags: o(a2.profile.tags), stamp: e$1.stamp })",
        },
      },
      {
        id: "rename_dom_renderer",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "h",
          },
        },
        target: {
          name: "renderSummaryDom",
        },
        fingerprint: {
          initStartsWith: "a2 => { const t2 = document.querySelector(\"#app\");",
        },
      },
      {
        id: "rename_app_renderer",
        operation: "rename_binding",
        selector: {
          chunkId: "static/index-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "g",
          },
        },
        target: {
          name: "renderDashboard",
        },
        fingerprint: {
          initStartsWith: "async () => { const a2 = o$1();",
        },
      },
      {
        id: "rename_format_activity_badge",
        operation: "rename_binding",
        selector: {
          chunkId: "static/chunk-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "e",
          },
        },
        target: {
          name: "formatActivityBadge",
        },
        fingerprint: {
          initStartsWith: "(t, a) => `${t.name}:${a.total}`",
        },
      },
      {
        id: "rename_format_chip_text",
        operation: "rename_binding",
        selector: {
          chunkId: "static/chunk-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "o",
          },
        },
        target: {
          name: "formatChipText",
        },
        fingerprint: {
          initStartsWith: "t => `chip:${t.stamp}`",
        },
      },
      {
        id: "rename_activity_badge_import",
        operation: "rename_binding",
        selector: {
          chunkId: "static/ActivityPanel-DuckMock",
          file: "entry.js",
          binding: {
            kind: "ImportSpecifier",
            name: "e",
          },
          import: {
            imported: "e",
            source: "../chunk-DuckMock/entry.js",
          },
        },
        target: {
          name: "formatActivityBadge",
        },
        fingerprint: {
          imported: "e",
          local: "e",
          source: "../chunk-DuckMock/entry.js",
        },
      },
      {
        id: "rename_activity_panel",
        operation: "rename_binding",
        selector: {
          chunkId: "static/ActivityPanel-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "o",
          },
        },
        target: {
          name: "renderActivityPanel",
        },
        fingerprint: {
          initStartsWith: "(t, a) => { const o2 = { badge: e(t, a), stamp: a.stamp, tags: t.tags.join(\",\") };",
        },
      },
      {
        id: "rename_chip_text_import",
        operation: "rename_binding",
        selector: {
          chunkId: "static/SummaryChip-DuckMock",
          file: "entry.js",
          binding: {
            kind: "ImportSpecifier",
            name: "o$1",
          },
          import: {
            imported: "o",
            source: "../chunk-DuckMock/entry.js",
          },
        },
        target: {
          name: "formatChipText",
        },
        fingerprint: {
          imported: "o",
          local: "o$1",
          source: "../chunk-DuckMock/entry.js",
        },
      },
      {
        id: "rename_summary_chip",
        operation: "rename_binding",
        selector: {
          chunkId: "static/SummaryChip-DuckMock",
          file: "entry.js",
          binding: {
            kind: "VariableDeclarator",
            name: "o",
          },
        },
        target: {
          name: "renderSummaryChip",
        },
        fingerprint: {
          initStartsWith: "t => { const o2 = { text: o$1(t) };",
        },
      },
    ],
    pipeline: [
      {
        id: "load_mock_bundle",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath,
        },
      },
      {
        id: "parse_mock_bundle",
        operation: "compute_js_asts",
      },
      {
        id: "split_mock_bundle",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "rename_mock_bundle",
        operation: "rename_bindings",
      },
      {
        id: "rewrite_mock_bundle_chunk_links",
        operation: "rewrite_chunk_entry_specifiers",
      },
      {
        id: "write_mock_bundle",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: transformedRoot,
        },
      },
      {
        id: "emit_mock_harness",
        operation: "emit_browser_harness",
        args: {
          assetSummaryPath,
          force: true,
          outDir: appRoot,
          scriptSource: "split",
          snapshotRoot,
        },
      },
    ],
  };
}
