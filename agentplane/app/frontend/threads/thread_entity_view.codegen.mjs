export default {
  plugins: [
    {
      name: "zod",
      $resolvers: {
        object: (ctx) => {
          if (ctx.schema.additionalProperties?.type !== "never") return undefined;
          return ctx.$(ctx.plugin.imports.z).attr("strictObject").call(ctx.nodes.shape(ctx));
        },
      },
    },
  ],
};
