FROM inventree/inventree:stable
COPY rai_plugin*.whl /tmp/
RUN pip install /tmp/rai_plugin*.whl && rm /tmp/rai_plugin*.whl
