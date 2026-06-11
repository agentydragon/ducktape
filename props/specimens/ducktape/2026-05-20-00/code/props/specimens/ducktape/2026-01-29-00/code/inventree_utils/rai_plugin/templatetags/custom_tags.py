"""
{% load custom_tags %}
Serial (masked): {{ stock.serial|obfuscate_serial }}
<br>
<img src="{% generate_qr stock.serial %}" alt="QR code for serial">


filter: 1 or 2 args -> return value for insertion -- string / safe HTML / ...

simple_tag: immediately inserted
inclusion_tag: call function, use return value to render sub-template (partial with local context etc.)

"advanced custom tags" -- custom subclasses of Node
    manual argument parsing in `parse`, render with `render`. anything -- incl. conditionals etc.

context processors -- already exposed by inventree plugin system
template loaders -- not useful for me
"""

import functools
import inspect
import logging
from typing import Any, ClassVar

from django import template
from django.utils.html import escape
from django.utils.safestring import SafeString

register = template.Library()
logger = logging.getLogger(__name__)


def shorten(value):
    """
    Shortens a physical value.

    '5 V' -> '5V'
    """
    if value is None:
        return None
    return value.replace(" ", "")


def prefix(x):
    return lambda y: x + y


def remove_prefix(x):
    return lambda y: y.removeprefix(x)


def fmt_range(min, max):
    if min and not max:
        return f"&geq;{min}"
    if max and not min:
        return f"&leq;{max}"
    if min and max:
        return f"&in;[{min};{max}]"
    return None


def apply(value, *fns):
    for fn in fns:
        if not value:
            break
        value = fn(value)
    return value


class ParametersProcessor:
    _displayers: ClassVar[list[tuple[tuple[str, ...], Any]]] = []

    def __init__(self, part, parameters):
        self.part = part
        self.parameters = {k: v for k, v in parameters.items() if v is not None}
        self.used = set()

    def get(self, x):
        if not self.parameters.get(x):
            return None
        self.used.add(x)
        return escape(self.parameters[x])

    @property
    def any_remaining(self):
        return bool(set(self.parameters) - self.used)

    @property
    def remaining_items(self):
        remaining_keys = set(self.parameters) - self.used
        for attrs, displayer in self._displayers:
            if any(attr in remaining_keys for attr in attrs):
                yield displayer(self)

        remaining_keys = set(self.parameters) - self.used
        for key in self.parameters:
            if key in remaining_keys and key not in ("Bought for", "Marking"):
                yield SafeString(f"{key}: {self.get(key)}")

    @staticmethod
    def displayer(*attrs):
        def decorator(fn):
            logger.error(f"Not callable: {fn}")

            @functools.wraps(fn)
            def wrapped(self, *args, **kwargs):
                out = fn(self, *(self.get(x) for x in attrs), *args, **kwargs)
                return SafeString(out) if out is not None else None

            frame = inspect.currentframe().f_back  # Get the caller frame (class body)
            frame.f_locals["_displayers"].append((attrs, wrapped))  # Get class-local variables
            return wrapped

        return decorator

    @displayer("Package")
    def package(self, val):
        return apply(val, remove_prefix("SMD "))

    @displayer("Power rating")
    def show_power_rating(self, val):
        return apply(val, shorten, prefix("P&lt;"))

    @displayer("Collector-emitter voltage (V_CEO)")
    def collector_emitter_voltage(self, val):
        return apply(val, shorten, prefix("V<sub>CEO</sub>="))

    @displayer("Resistance", "Tolerance")
    def show_value(self, resistance, tolerance):
        val = apply(resistance, shorten)
        tol = apply(tolerance, prefix("±"))
        return val + tol

    @property
    def heading(self):
        return self.get("Jellybean P/N") or self.part.name

    def under_category(self, name):
        category = self.part.category
        while category:
            if category.name == name:
                return True
            category = category.parent
        return False

    @property
    def is_ldo(self):
        return self.under_category("Linear LDO voltage regulators")

    @property
    def is_bjt(self):
        return self.under_category("BJT")

    @property
    def is_ic(self):
        return self.under_category("Integrated circuits")

    @property
    def is_resistor(self):
        return self.under_category("Resistors")

    @property
    def is_resistor_network(self):
        return self.under_category("Resistor networks")

    @property
    def is_diode(self):
        return self.under_category("Diodes")

    @displayer("Minimum input voltage", "Maximum input voltage")
    def input_voltage_range(self, min, max):
        min, max = shorten(min), shorten(max)
        return apply(fmt_range(min, max), prefix("V<sub>in</sub>"))

    @displayer("Reverse voltage")
    def reverse_voltage(self, val):
        # Or "V_rev"
        return apply(val, shorten, prefix("V<sub>R</sub>="))

    @displayer("Power dissipation (Pd)")
    def power_dissipation(self, val):
        return apply(val, shorten, prefix("P<sub>d</sub>="))

    @displayer("Forward voltage @ current")
    def forward_voltage_at_current(self, val):
        val = apply(val, shorten)
        if not val:
            return None
        voltage, current = val.split("@")
        return f"V<sub>F</sub>={shorten(voltage)}@{shorten(current)}"

    @displayer("Average rectified forward current (I_F)")
    def average_rectified_forward_current(self, val):
        return apply(val, shorten, prefix("I<sub>F</sub>="))

    @displayer("Reverse leakage current @ voltage (I_R)")
    def reverse_leakage_current(self, val):
        val = apply(val, shorten)
        if not val:
            return None
        ir, v = val.split("@")
        return f"I<sub>R</sub>={shorten(ir)}@{shorten(v)}"

    @displayer("Minimum supply voltage", "Maximum supply voltage")
    def supply_voltage_range(self, min, max):
        min, max = shorten(min), shorten(max)
        return apply(fmt_range(min, max), prefix("V<sub>sup</sub>"))

    @displayer("Maximum output current")
    def max_output_current(self, val):
        return apply(fmt_range(None, shorten(val)), prefix("I<sub>out</sub>"))

    @displayer("Output voltage")
    def output_voltage(self, val):
        # TODO: voltage can also here be written as 'adjustable 1.2-37 V',
        # 'adjustable 0.8~5.5V'
        return shorten(val).replace("adjustable", "adj")

    @displayer("BJT type")
    def bjt_type(self, val):
        return val

    @displayer("Function")
    def function(self, val):
        return val

    @displayer("Jellybean P/N")
    def jellybean_pn(self, val):
        return val


@register.simple_tag(takes_context=True)
def parameters_processor(context):
    return ParametersProcessor(context["part"], context["parameters"])


####
#
## myapp/templatetags/mytags.py
#
# class GroupByFieldNode(template.Node):
#        """
#        usage: {% load mytags %}
#
#        {% group_by_field my_items "category" as cat items %}
#          <h2>Category: {{ cat }}</h2>
#          <ul>
#            {% for item in items %}
#              <li>{{ item.name }}</li>
#            {% endfor %}
#          </ul>
#        {% endgroup_by_field %}
#        """
#    def __init__(self, nodelist, list_var, field_name, group_var, items_var):
#        self.nodelist = nodelist
#        self.list_var = template.Variable(list_var)
#        self.field_name = field_name
#        self.group_var = group_var
#        self.items_var = items_var
#
#    def render(self, context):
#        try:
#            items = self.list_var.resolve(context)
#        except template.VariableDoesNotExist:
#            return ""
#
#        # Group items by the chosen field
#        groups = {}
#        for item in items:
#            key = getattr(item, self.field_name, None)
#            groups.setdefault(key, []).append(item)
#
#        output = []
#        for key, group_items in groups.items():
#            context.push()
#            # Expose group key & items in the context
#            context[self.group_var] = key
#            context[self.items_var] = group_items
#            # Render the nested template content
#            output.append(self.nodelist.render(context))
#            context.pop()
#
#        return "".join(output)
#
#
# @register.tag
# def group_by_field(parser, token):
#    """
#    Usage:
#      {% group_by_field items "field_name" as group_key_var items_var %}
#          <!-- template code referencing group_key_var, items_var -->
#      {% endgroup_by_field %}
#    """
#    bits = token.split_contents()
#    if len(bits) != 6 or bits[2].startswith('"') is False:
#        raise template.TemplateSyntaxError(
#            'Usage: {% group_by_field items "field" as group_key_var items_var %}'
#        )
#    _, list_var, field_name_quoted, _, group_var, items_var = bits
#    field_name = field_name_quoted.strip("\"'")  # remove quotes
#
#    # parse everything inside {% group_by_field ... %} ... {% endgroup_by_field %}
#    nodelist = parser.parse(("endgroup_by_field",))
#    parser.delete_first_token()
#
#    return GroupByFieldNode(nodelist, list_var, field_name, group_var, items_var)
#
#
########
#
## you can take context:
#
# @register.simple_tag(takes_context=True)
# def random_greeting(context, user_id):
#    """
#    Demonstrates a custom DB query plus random logic,
#    then returns a string inserted into the template.
#    """
#    import random
#
#    from myapp.models import UserProfile
#    greetings = [
#        "Hello", "Ahoy", "Welcome", "Greetings", "What's up", "Hey there"
#    ]
#    try:
#        profile = UserProfile.objects.get(pk=user_id)
#        greet = random.choice(greetings)
#        return f"{greet}, {profile.user.username}!"
#    except UserProfile.DoesNotExist:
#        return "User not found."
#
######
#
# @register.inclusion_tag('components/user_panel.html', takes_context=True)
# def user_panel(context, user_id):
#    """
#    Renders a subtemplate with a custom context:
#    e.g., a user panel showing a user's name, avatar, etc.
#        {% if profile %}
#          <div class="user-panel">
#            <img src="{{ profile.avatar_url }}" alt="Avatar" />
#            <p>Welcome, {{ profile.user.username }}!</p>
#          </div>
#        {% else %}
#          <p>No user panel to display.</p>
#        {% endif %}
#
#    """
#    from myapp.models import UserProfile
#    try:
#        profile = UserProfile.objects.get(pk=user_id)
#    except UserProfile.DoesNotExist:
#        profile = None
#
#    # Returns a dict of context used by components/user_panel.html
#    return {
#        'profile': profile,
#        'request': context['request']  # pass request if subtemplate needs it
#    }


# using the entire context
# @register.simple_tag(takes_context=True)
# def user_message(context):
#    user = context['request'].user
#    return f"Welcome back, {user.username}!" if user.is_authenticated else "Hello, guest!"


# @register.filter
# def obfuscate_serial(value):
#    """Mask a serial number, revealing only the last 4 characters."""
#    s = str(value)
#    if len(s) <= 4:
#        return s
#    # Replace leading characters with '*', keep last 4
#    masked = "*" * (len(s) - 4) + s[-4:]
#    return masked
# @register.simple_tag
# def generate_qr(data):
#    """Generate a QR code image (PNG format) for the given data and return as data URI."""
#    # Create QR code image using the qrcode library
#    img = qrcode.make(str(data))
#    buffer = BytesIO()
#    img.save(buffer, format="PNG")
#    img_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
#    # Return an inline image (data URI)
#    return f"data:image/png;base64,{img_b64}"
