def add_label_context(self, label_instance, model_instance, request, context):
    """Add extra context to the provided label instance.

    By default, this method does nothing.

    Args:
        label_instance: The label instance to add context to
        model_instance: The model instance which initiated the label generation
        request: The request object which initiated the label generation
        context: The context dictionary to add to
    """

    # model_instance: StockItem
    # request: ... probably a Django request?
    # context: dict, will add stuff in there

    # this is how it's eventually used:

    # wp = WeasyprintReport(
    #     request,
    #     self.template_name,
    #     base_url=get_base_url(request=request),
    #     presentational_hints=True,
    #     filename=self.generate_filename(context),
    #     **kwargs,
    # )
    # return wp.render_to_response(context, **kwargs)

    # report snippets: https://docs.inventree.org/en/0.17.1/report/templates/#report-snippets
    #   allows calling sub-templates
