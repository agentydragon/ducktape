from cdk8s import App, Chart
from schema_codegen_crds.test.example.schema.codegen import Widget, WidgetSpec


def widget_chart(app: App) -> Widget:
    chart = Chart(app, "widgets")
    spec = WidgetSpec(title="example", replicas=2)
    return Widget(chart, "example", spec=spec)
