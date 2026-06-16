const selectedComponent = wrap(function (props) {
  const theme = useTheme(),
    locale = useLocale(),
    { getCommand: getCommand } = useCommands(),
    {
      content: content,
      node: node,
      parentNode: parentNode,
      nodePath: nodePath,
      computedChildren: computedChildren,
      deletable: deletable,
      referenceContext: referenceContext,
      tableCellContext: tableCellContext,
      placeholderText: placeholderText,
      isStatic: isStatic,
      spellCheck: spellCheck,
      cardLayout: cardLayout,
    } = props;
  const formatted = useMemo(() => format(content, { locale: locale, theme: theme }), [content, locale, theme]);
  const handleClick = useCallback(
    (event) => {
      event.preventDefault();
      getCommand("open").run(node);
    },
    [getCommand, node]
  );
  return jsx("div", {
    className: "uniqueDiscriminatorCard",
    onClick: handleClick,
    children: jsx(Body, { node: node, formatted: formatted }),
  });
});

export { selectedComponent };
