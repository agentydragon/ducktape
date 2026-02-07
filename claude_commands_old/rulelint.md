<prompt version="1.0">
  <meta>
    <title>🔍 /rulelint - Rule and Prompt Quality Validator</title>
    <complexity level="medium" />
    <domain>meta</domain>
    <tags>linting, validation, meta-prompting</tags>
  </meta>

  <context>
    <purpose>Validate rules, prompts, and instructions against LLM best practices and ensure they follow established patterns for maximum effectiveness. Also validates the semi-formal XML markup structure used in Claude prompts (not strict XML validation, but checks for well-formed pseudo-XML with matching tags, proper nesting, and correct reference formats).</purpose>

    <command-syntax><![CDATA[

/rulelint [file|text] [path/content]
]]></command-syntax>

    <schema-reference>
      This linter validates against the <a href="~/.claude/schemas/prompt-xml-schema.md">Claude Prompt XML Schema</a> which defines:
      <ul>
        <li>Semi-formal XML structure (not strict XML validation)</li>
        <li>Proper opening/closing tag matching</li>
        <li><![CDATA[Tool call formats (<tool-call>, <mcp>, etc.)]]></li>
        <li><![CDATA[Conversation patterns (<u>, <a>, <t> or <message from="">)]]></li>
        <li><![CDATA[Good/bad marking conventions (<good>, <bad>, <critical>)]]></li>
        <li><![CDATA[Hierarchical rule organization (id="/code/python/types")]]></li>
      </ul>
    </schema-reference>

  </context>

  <rules>
    <rule id="/validation/semi-formal-xml">
      <title>✅ Semi-formal XML Structure</title>
      <content>
        Validates well-formed pseudo-XML markup used in Claude prompts. This is NOT strict XML validation - it allows informal attributes like <![CDATA[<example negative>]]> without quotes. Checks include:
        <ul>
          <li>Opening and closing tags must match</li>
          <li>Tags should be properly nested (no overlapping)</li>
          <li>References must use correct format: <![CDATA[<ref href="#..."/>]]> or <![CDATA[<ref href="~/.claude/..."/>]]></li>
          <li>Examples should be tagged as positive/negative</li>
          <li>Tool calls must follow schema format</li>
          <li>No orphaned or mismatched tags</li>
        </ul>
      </content>
      <example negative>
        <![CDATA[
<example>  <!-- Missing positive/negative qualifier -->
  <u>What's 2+2?</u>
  <a>4</example>  <!-- Mismatched closing tag -->
        ]]>
      </example>
      <example positive>
        <![CDATA[
<example positive>
  <u>What's 2+2?</u>
  <a>4</a>
</example>  <!-- Properly matched tags -->
        ]]>
      </example>
    </rule>

    <rule id="/validation/tool-calls">
      <title>✅ Tool Call Format (per XML Schema)</title>

      <section id="old-format">
        <title>Old Claude Code Format</title>
        <example negative>
          <![CDATA[mcp__brave-search__brave_web_search "query text"]]>
        </example>
        <example positive>
          <![CDATA[

Tool: mcp**brave-search**brave_web_search
Parameters:
  query: "query text"
  count: 10
          ]]>
        </example>
      </section>

      <section id="xml-format">
        <title>New XML Schema Format</title>
        <example positive>
          <![CDATA[

<tool-call>
  <mcp server="brave-search" tool="brave_web_search">
    <params>
      {
        "query": "query text",
        "count": 10
      }
    </params>
  </mcp>
</tool-call>

<!-- Or for simple tools -->
<tool-call>
  <read path="/src/main.py" />
  <bash command="git status" />
</tool-call>
          ]]>
        </example>
      </section>
    </rule>

    <rule id="/validation/conversation">
      <title>✅ Conversation Format (per XML Schema)</title>

      <examples>
        <example negative>
          <![CDATA[User: text / Assistant: response]]>
        </example>

        <example positive>
          <![CDATA[

U: What's 2+2?
A: 4
]]>
</example>

        <example positive>
          <![CDATA[

<conversation>
  <message from="user">What's 2+2?</message>
  <message from="assistant">4</message>
</conversation>
          ]]>
        </example>

        <example positive>
          <![CDATA[
<u>What's 2+2?</u>
<a>4</a>
<t>Tool output here</t>
          ]]>
        </example>
      </examples>
    </rule>

    <rule id="/validation/situation-action">
      <title>✅ Situation → Action Pattern</title>
      <example negative>
        <content>Use grep for searching</content>
      </example>
      <example positive>
        <content>PATTERN: search_needed → Grep > manual scanning</content>
      </example>
      <example positive>
        <content>When searching for text patterns in files, use Grep tool instead of reading files manually</content>
      </example>
    </rule>

    <rule id="/validation/examples">
      <title>✅ Positive and Negative Examples</title>
      <content>
        <![CDATA[

<examples>
  <example negative>
    <code language="python">
    # Bad: Silent failure
    try:
        process()
    except:
        pass
    </code>
  </example>

  <example positive>
    <code language="python">
    # Good: Explicit error handling
    try:
        process()
    except ProcessError as e:
        logger.error(f"Failed: {e}")
        raise
    </code>
  </example>
</examples>
        ]]>
      </content>
    </rule>

    <rule id="/validation/triggers">
      <title>✅ Trigger Patterns</title>
      <content>
        REQUIRED: Clear triggers that prompt the rule
        <ul>
          <li>ERROR(pattern) → action</li>
          <li>REPEAT(3) → automate</li>
          <li>STUCK → search_learnings</li>
          <li>FILE_COUNT(>20) → use_glob</li>
        </ul>
      </content>
    </rule>

    <rule id="/validation/interlinking">
      <title>✅ Rule Interlinking</title>
      <content>
        <![CDATA[

<!-- Define rules with hierarchical IDs -->
<rule id="/code/python/types/new-style-optional">
  <title>Use New Style Optional</title>
  <content>...</content>
</rule>

<!-- Reference them -->
Follow <ref href="#/code/python/types/new-style-optional" />
See <a href="#/code/quality">code quality rules</a>
When needed, <call href="#validate-inputs" />
        ]]>
      </content>
    </rule>

    <rule id="/validation/anthropic-practices">
      <title>✅ Anthropic Best Practices</title>

      <rule id="/validation/anthropic-practices/clear-direct">
        <title>Be Clear and Direct</title>
        <source>Anthropic Prompt Engineering Guide</source>
        <quote>Use precise, unambiguous language. Explicitly state your expectations.</quote>
        <example negative>
          <content>Handle errors appropriately</content>
        </example>
        <example positive>
          <content>When encountering FileNotFoundError, check path exists with Path.exists() before proceeding</content>
        </example>
      </rule>

      <rule id="/validation/anthropic-practices/xml-tags">
        <title>Leverage XML Tags</title>
        <source>Anthropic Prompt Engineering Guide</source>
        <quote>Use structured XML tags to delineate different prompt components</quote>
        <example positive>
          <![CDATA[

<situation>
When user requests file creation
</situation>
<action>
1. Check if file exists
2. Warn if overwriting
3. Create with explicit content
</action>
          ]]>
        </example>
      </rule>

      <rule id="/validation/anthropic-practices/chain-of-thought">
        <title>Enable Reasoning (Chain of Thought)</title>
        <source>Anthropic Prompt Engineering Guide</source>
        <quote>Break complex tasks into step-by-step reasoning</quote>
        <example positive>
          <content>THINK: Is this a search task? → Yes → Use Agent tool for parallel search</content>
        </example>
        <example positive>
          <content>First understand X, then apply Y, finally verify Z</content>
        </example>
      </rule>

      <rule id="/validation/anthropic-practices/multishot">
        <title>Use Examples (Multishot Prompting)</title>
        <source>Anthropic Prompt Engineering Guide</source>
        <quote>Include 2-3 example inputs/outputs to demonstrate desired format</quote>
        <example positive>
          <content>Always show concrete <![CDATA[<u>/<a>]]> examples with actual commands and responses</content>
        </example>
      </rule>

      <rule id="/validation/anthropic-practices/system-prompts">
        <title>Provide System Prompts</title>
        <source>Anthropic Prompt Engineering Guide</source>
        <quote>Give Claude a specific role or persona. Set context and behavioral guidelines.</quote>
        <example positive>
          <content>You are a code quality guardian. Your role is to...</content>
        </example>
        <example positive>
          <content>CONTEXT: In large codebases (>1000 files)...</content>
        </example>
        <example positive>
          <content>ASSUMES: User has Git repository initialized</content>
        </example>
      </rule>
    </rule>

    <rule id="/validation/realistic-examples">
      <title>✅ Realistic, Executable Examples</title>
      <example negative>
        <content>Search for the thing</content>
      </example>
      <example positive>
        <conversation>
          <u>Find all TODO comments in the codebase</u>
          <a>I'll search for TODO comments across all files.

          <tool-call>
            <grep pattern="TODO|FIXME|XXX" include="*.py" />
          </tool-call>
          </a>
        </conversation>
      </example>
    </rule>

    <rule id="/validation/agentic-intelligence">
      <title>✅ Agentic Intelligence</title>
      <content>Show smart decision-making:</content>
      <example positive>
        <![CDATA[
If search returns >100 results → narrow with more specific pattern
If search returns 0 results → broaden pattern or check file extensions
If pattern has special chars → escape for regex
        ]]>
      </example>
    </rule>

    <rule id="/validation/compatibility">
      <title>✅ Compatibility Check</title>
      <ul>
        <li>No contradictions with existing rules</li>
        <li>Consistent terminology</li>
        <li>Compatible trigger patterns</li>
        <li>Non-overlapping anchors</li>
      </ul>
    </rule>

    <rule id="/validation/second-person">
      <title>No Second Person for User</title>
      <content>Never use "you" or "your" to refer to the user in documentation or commands. Use "the user" or rephrase.</content>
      <example negative>
        <content>You can use this command when you need to clean up files</content>
      </example>
      <example positive>
        <content>The user can use this command when cleanup is needed</content>
      </example>
    </rule>

  </rules>

  <process id="linting-process">
    <title>Linting Process</title>

    <step priority="0">
      <title>Validate Semi-formal XML</title>
      <ul>
        <li>Check opening/closing tag matches</li>
        <li>Allow informal attribute syntax (e.g., <![CDATA[<example negative>]]>)</li>
        <li>Validate reference formats (<![CDATA[<ref href="#..."/>]]> or file paths)</li>
        <li>Ensure example tags have positive/negative qualifier</li>
        <li>Check tool call follows schema patterns</li>
        <li>Find orphaned or overlapping tags</li>
      </ul>
    </step>

    <step priority="1">
      <title>Parse Structure</title>
      <ul>
        <li>Identify triggers</li>
        <li>Extract examples</li>
        <li>Find action patterns</li>
        <li>Locate interlinks</li>
      </ul>
    </step>

    <step priority="2">
      <title>Check Completeness</title>
      <ul>
        <li>Has positive examples? ✓/✗</li>
        <li>Has negative examples? ✓/✗</li>
        <li>Has clear triggers? ✓/✗</li>
        <li>Has situation→action? ✓/✗</li>
        <li>Has realistic demos? ✓/✗</li>
      </ul>
    </step>

    <step priority="3">
      <title>Validate Quality</title>
      <ul>
        <li>Specific not vague? ✓/✗</li>
        <li>Actionable steps? ✓/✗</li>
        <li>Tool calls correct? ✓/✗</li>
        <li>Examples executable? ✓/✗</li>
      </ul>
    </step>

    <step priority="4">
      <title>Test Compatibility</title>
      <ul>
        <li>Check against CLAUDE.md rules</li>
        <li>Verify anchor uniqueness</li>
        <li>Ensure terminology consistency</li>
      </ul>
    </step>

    <step priority="5">
      <title>Fix Issues (Progressive Approach)</title>
      <content>
        When issues are found, fix them in order from easiest to most complex:

        <phase id="phase-1-trivial">
          <title>Phase 1: Trivial, Non-controversial Fixes</title>
          <ul>
            <li>Fix mismatched XML tags (e.g., <![CDATA[<example>...</bad>]]> → <![CDATA[<example>...</example>]]>)</li>
            <li>Add missing positive/negative qualifiers to examples</li>
            <li>Fix broken reference formats (<![CDATA[<ref="#anchor">]]> → <![CDATA[<ref href="#anchor"/>]]>)</li>
            <li>Correct obvious typos in tags</li>
            <li>Add missing closing tags</li>
            <li>Fix CDATA sections that aren't properly closed</li>
          </ul>
        </phase>

        <phase id="phase-2-formatting">
          <title>Phase 2: Formatting and Structure</title>
          <ul>
            <li>Convert old tool call formats to new XML schema format</li>
            <li>Update conversation formats (User:/Assistant: → <![CDATA[<u>/<a>]]>)</li>
            <li>Properly indent nested XML structures</li>
            <li>Group related examples under <![CDATA[<examples>]]> tags</li>
            <li>Add language attributes to code blocks</li>
          </ul>
        </phase>

        <phase id="phase-3-content-improvements">
          <title>Phase 3: Content Enhancement</title>
          <ul>
            <li>Add missing positive examples where only negative exist</li>
            <li>Add missing negative examples where only positive exist</li>
            <li>Convert vague instructions to specific situation→action patterns</li>
            <li>Add clear triggers where missing</li>
            <li>Make examples more realistic and executable</li>
          </ul>
        </phase>

        <phase id="phase-4-structural-changes">
          <title>Phase 4: Major Structural Improvements</title>
          <ul>
            <li>Reorganize content into proper rule hierarchy</li>
            <li>Add interlinking between related rules</li>
            <li>Create proper id attributes for cross-referencing</li>
            <li>Extract sub-rules from overly complex rules</li>
            <li>Add reasoning/explanation sections</li>
          </ul>
        </phase>
      </content>

      <fix-protocol>
        <title>Fix Protocol</title>
        <ol>
          <li>Run full lint analysis first</li>
          <li>Group issues by phase (1-4)</li>
          <li>Apply fixes incrementally:
            <ul>
              <li>Make Phase 1 fixes (show diff)</li>
              <li>Make Phase 2 fixes (show diff)</li>
              <li>Make Phase 3 fixes (show diff)</li>
              <li>Make Phase 4 fixes (show diff)</li>
            </ul>
          </li>
          <li>After each phase, re-lint to verify improvements</li>
          <li>Show before/after score comparison</li>
        </ol>
      </fix-protocol>
    </step>

  </process>

  <output>
    <section id="lint-report-example">
      <title>Example Lint Report</title>
      <![CDATA[
🔍 RULE LINT REPORT: no-disabling-code-quality-checks.md

✅ PASSED:

- Clear triggers (mypy errors, ESLint warnings)
- Situation→Action patterns throughout
- Positive/negative examples with explanations
- MCP tool calls properly formatted
- Interlinked with anchors
- Realistic, executable examples

⚠️ WARNINGS:

- Line 315: Example could be more specific
- Missing link to #optimal-grip pattern

❌ FAILURES:
None

SCORE: 95/100 - Excellent rule quality
      ]]>
    </section>
  </output>

  <common-issues>
    <issue id="vague-instructions">
      <title>Vague Instructions</title>
      <example negative>Handle the error gracefully</example>
      <example positive>Catch ValueError, log with context, return None</example>
    </issue>

    <issue id="missing-triggers">
      <title>Missing Triggers</title>
      <example negative>Use this pattern for optimization</example>
      <example positive>When profiler shows function takes >1s, apply this pattern</example>
    </issue>

    <issue id="poor-examples">
      <title>Poor Examples</title>
      <example negative>
        <code language="python">foo(bar)  # No context</code>
      </example>
      <example positive>
        <conversation>
          <u>My DataFrame merge is taking 5 minutes</u>
          <a>I'll optimize using merge keys...
          <code language="python">
          # Use merge on indexed columns
          df1_indexed = df1.set_index('key_column')
          df2_indexed = df2.set_index('key_column')
          result = df1_indexed.merge(df2_indexed, left_index=True, right_index=True)
          </code>
          </a>
        </conversation>
      </example>
    </issue>

    <issue id="no-reasoning">
      <title>No Reasoning</title>
      <example negative>Always use pandas</example>
      <example positive>For CSV files >100MB, use pandas for memory-efficient processing. For smaller files, csv.reader is sufficient.</example>
    </issue>

  </common-issues>

  <integration>
    <title>Integration with CLAUDE.md</title>
    <content>
      This linter enforces patterns from:
      <ul>
        <li><ref href="#triggers" /> - Universal trigger patterns</li>
        <li><ref href="#optimal-grip" /> - Right abstraction level</li>
        <li><ref href="#no-redundant-docs" /> - Clear, non-obvious docs only</li>
        <li><ref href="#instruction-update" /> - Quality standards for rules</li>
      </ul>
    </content>
  </integration>

  <usage-examples>
    <title>Usage Examples</title>
    <![CDATA[
/rulelint file ~/.claude/modules/new-rule.md
/rulelint text "When error occurs, fix it"
/rulelint validate @{#no-disabling-code-quality-checks}
    ]]>
  </usage-examples>

  <quality-metrics>
    <title>Quality Metrics</title>
    <content>
      A rule scores points for:
      <ul>
        <li>Clear triggers (20 pts)</li>
        <li>Positive examples (15 pts)</li>
        <li>Negative examples (15 pts)</li>
        <li>Situation→Action (15 pts)</li>
        <li>Tool format correctness (10 pts)</li>
        <li>Interlinking (10 pts)</li>
        <li>Reasoning/explanation (10 pts)</li>
        <li>Compatibility (5 pts)</li>
      </ul>

      Target: 80+ points for acceptance
    </content>

  </quality-metrics>

  <footer>
    <note>Remember: Great rules are specific, actionable, and demonstrable. They show, not just tell.</note>

    <references>
      <ref id="anthropic-guide">Anthropic. (2024). <cite>Prompt Engineering Guide</cite>. https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/overview</ref>
    </references>

  </footer>
</prompt>
