export function statementShape(statement) {
  if (statement.type === "VariableDeclaration") {
    const initializers = new Set(statement.declarations.map((declaration) => declaration.init?.type ?? "none"));
    return `${statement.kind} ${[...initializers].join("|")}`;
  }
  if (statement.type === "ExpressionStatement") {
    const expression = statement.expression;
    if (expression.type === "AssignmentExpression") return `assign ${expression.left.type}=${expression.right.type}`;
    if (expression.type === "CallExpression") return `call ${expression.callee.type}`;
    return expression.type;
  }
  return statement.type;
}
