from inventree.api import InvenTreeAPI
from inventree.base import Parameter, ParameterTemplate
from inventree.part import Part


def part_url(api: InvenTreeAPI, part: Part) -> str:
    base = api.base_url.rstrip("/")
    return f"{base}/part/{part.pk}/"


def list_part_parameters(api: InvenTreeAPI, **filters: int) -> list[Parameter]:
    rows: list[Parameter] = Parameter.list(api, model_type=Part.getModelType(), limit=0, raise_error=True, **filters)
    return rows


def create_part_parameter(api: InvenTreeAPI, part: Part, template: ParameterTemplate, value: str) -> None:
    Parameter.create(
        api, {"model_type": Part.getModelType(), "model_id": part.pk, "template": template.pk, "data": value}
    )
