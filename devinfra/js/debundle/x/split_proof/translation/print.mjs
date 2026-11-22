import { EXPRESSIONS_PRECEDENCE, GENERATOR, NEEDS_PARENTHESES, generate } from "astring";

function writeNode(node, state) {
  state.generator[node.type](node, state);
}

// Astring 1.9 reads `.name` for module export names and attribute keys, which
// Acorn can also represent as string Literals. Give it the printed literal.
function moduleName(node) {
  return node?.type === "Literal" ? { ...node, name: generate(node) } : node;
}

function parenthesized(expression) {
  return { type: "ParenthesizedExpression", expression };
}

function writeParenthesized(node, state) {
  state.write("(");
  writeNode(node.expression, state);
  state.write(")");
}

const expressionsPrecedence = {
  ...EXPRESSIONS_PRECEDENCE,
  ImportExpression: NEEDS_PARENTHESES,
  ParenthesizedExpression: EXPRESSIONS_PRECEDENCE.SequenceExpression,
};

export function printProgram(program, source) {
  if (program.type !== "Program" || program.sourceType !== "module")
    throw new Error("expected an ESTree module Program");

  // ECMAScript exposes a callable's exact source, including comments and whitespace,
  // through Function.prototype.toString. The splitter moves these nodes intact.
  function writeCallable(node, state) {
    const { start, end } = node;
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > source.length)
      throw new Error(`expected a source range for ${node.type}`);
    state.write(source.slice(start, end));
  }

  const generator = {
    ...GENERATOR,
    FunctionDeclaration: writeCallable,
    FunctionExpression: writeCallable,
    ArrowFunctionExpression: writeCallable,
    ClassDeclaration: writeCallable,
    ClassExpression: writeCallable,
    MethodDefinition: writeCallable,
    ParenthesizedExpression: writeParenthesized,
    // A nested ChainExpression is an explicit short-circuit boundary.
    ChainExpression: writeParenthesized,
    ExpressionStatement(node, state) {
      const { expression } = node;
      // Keep ordinary string expressions out of a new directive prologue.
      const ordinaryString =
        node.directive === undefined && expression.type === "Literal" && typeof expression.value === "string";
      GENERATOR.ExpressionStatement.call(
        this,
        ordinaryString ? { ...node, expression: parenthesized(expression) } : node,
        state
      );
    },
    ImportExpression(node, state) {
      if (!node.options) {
        GENERATOR.ImportExpression.call(this, node, state);
        return;
      }
      state.write("import(");
      writeNode(node.source, state);
      state.write(", ");
      writeNode(node.options, state);
      state.write(")");
    },
    ImportDeclaration(node, state) {
      const specifiers = node.specifiers.map((specifier) =>
        specifier.type === "ImportSpecifier" ? { ...specifier, imported: moduleName(specifier.imported) } : specifier
      );
      GENERATOR.ImportDeclaration.call(this, { ...node, specifiers }, state);
    },
    ImportAttribute(node, state) {
      GENERATOR.ImportAttribute.call(this, { ...node, key: moduleName(node.key) }, state);
    },
    ExportNamedDeclaration(node, state) {
      const specifiers = node.specifiers.map((specifier) => ({
        ...specifier,
        local: moduleName(specifier.local),
        exported: moduleName(specifier.exported),
      }));
      GENERATOR.ExportNamedDeclaration.call(this, { ...node, specifiers }, state);
    },
    ExportAllDeclaration(node, state) {
      GENERATOR.ExportAllDeclaration.call(this, { ...node, exported: moduleName(node.exported) }, state);
    },
    ExportDefaultDeclaration(node, state) {
      const { declaration } = node;
      // Keep a function/class expression from being parsed as a declaration.
      const callableExpression = declaration.type === "FunctionExpression" || declaration.type === "ClassExpression";
      GENERATOR.ExportDefaultDeclaration.call(
        this,
        callableExpression ? { ...node, declaration: parenthesized(declaration) } : node,
        state
      );
    },
    ForOfStatement(node, state) {
      // An unparenthesized `async of` is reserved in a synchronous for-of head.
      const left =
        !node.await && node.left.type === "Identifier" && node.left.name === "async"
          ? parenthesized(node.left)
          : node.left;
      GENERATOR.ForOfStatement.call(this, { ...node, left }, state);
    },
  };
  const hashbang = program.hashbang === undefined ? "" : `${program.hashbang}\n`;
  return hashbang + generate(program, { generator, expressionsPrecedence });
}
