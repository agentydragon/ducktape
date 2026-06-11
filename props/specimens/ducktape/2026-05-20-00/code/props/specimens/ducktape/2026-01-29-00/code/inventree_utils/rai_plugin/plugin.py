import logging

from plugin import InvenTreePlugin
from plugin.mixins import AppMixin, ReportMixin

logger = logging.getLogger("inventree")


class RaiPlugin(AppMixin, ReportMixin, InvenTreePlugin):
    NAME = "RaiPlugin"
    SLUG = "raiplugin"
    TITLE = "Rai Plugin"
    DESCRIPTION = "Example plugin with custom template filter, tag, and label context."
    VERSION = "1.0"
    # AUTHOR = _("Your Name")

    def add_label_context(self, label_instance, model_instance, request, context):
        """Inject extra context variables into label templates."""
        # Example: Add a static message and (if applicable) masked serial number
        context["plugin_message"] = "Hello from ObfuscateQRPlugin"
        # If the model instance has a 'serial' attribute, provide an obfuscated version
        serial = getattr(model_instance, "serial", None)
        if serial is not None:
            s = str(serial)
            # Mask all but last 4 characters of the serial
            context["obfuscated_serial"] = s[-4:].rjust(len(s), "*")


"""

SMD 1x1 box:
    width available: about 12 characters

    integrated circuits:
        [[[ 74HC164 ]]]
        [SOIC-14] [8b SIPO shift register]

        Quoted strings are parameter names
        - "Jellybean name" (if present), or part name
        - "Package" parameter
        - "Function" parameter

        ----------------------

        [[[ LMV321 ]]]
        [SOT-23-5] [1x op-amp]
                                    <-- TODO: "rail to rail", "voltage range"?
        - "Jellybean name" (if present), or part name
        - "Package" parameter
        - "Function" parameter

        ----------------------

        [[[ FT230XQ-R ]]]
        [QFN-16] [USB-UART transceiver]

        - "Jellybean name" (if present), or part name
        - "Package" parameter
        - "Function" parameter

    LDOs:
        [[[ AMS1117CD-3.3 ]]]
        [TO-252-2]
        [LDO->3.3V]
        [Iout<1A] [Vin<15V]

        - "Jellybean name" (if present), or part name
        - "Package" parameter
        - "Output voltage"
        - "Maximum output current"


    resistor networks:
        RN 4x[[0402]] [[10kΩ±1%]]
        [[62.5mW]]

        - "Package"
        - "Resistance"
        - "Tolerance" (value of this parameter should be normalized - can include +- symbol, can include %)
        - "Power rating"

    inductors:
        L [[4x4mm]] [[6.8μH±20%]]
        [[I<2.3A]]

        - "Package"
        - "Inductance"
        - "Tolerance" (normalize)
        - "Current rating"

    tactile switch:
        [[DPST]] tact
        [[W5.1xL5.1xH1.5]]

    mosfets:
        [[ AO3400A ]]
        [SOT-23]
        [N-ch E-MOSFET]
        [Vds≤30V] [Id≤5.4A]  # TODO: any other parameters that should go here?

        - "Jellybean name" (if present), or part name
        - "Package" parameter
        - "MOSFET channel type" (N/P)
        - "MOSFET mode" (enhancement/depletion) -> "E" or "D"
        - "Drain-source voltage rating (breakdown voltage)"
        - "Continuous drain current (Id)"

    bjts:
        [[ BCM857BS ]]
        [SOT-23]
        [2xPNP]
        [Vceo≤50V] [Ic≤0.5A]  # TODO: any other parameters that should go here?

        - "Jellybean name" (if present), or part name
        - "Package" parameter
        - "BJT type" (NPN/PNP)
        - "Collector-emitter voltage (V_CEO)"
        - (TODO - collector current missing right now)


    diodes:
        [[ 1N4148 ]]
        [SOD-323]
        [Vf=1.25V@150mA] [Vr=100V]

        - "Jellybean name" (if present), or part name
        - "Package" parameter
        - "Forward voltage @ current" (value is e.g. 1.1 V@1A")
        - "Reverse voltage"

    usb connector: (going by https://gct.co/usb-connector/usb-orientations-and-mount-types)
        CONN [[USB-C]] SMD top mount, 12p
        TYPE-C-31-M-12
        (^-- TODO)


    potentiometer:
        <TODO>

    LED:
        LED (red) [[0603]]
        [[Vf=2.0V@20mA]]

    resettable fuse:

    various:
        [[ HX6286ESO ]]
        [SOT-23] [Hall switch]
                                   (maybe: trigger points, voltage range?, bipolar/unipolar?)

        ----------------------
"""
