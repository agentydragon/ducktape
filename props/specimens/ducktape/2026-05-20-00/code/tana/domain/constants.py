from tana.domain.types import NodeId

# Field/Attribute IDs
SUPERTAG_KEY_ID = NodeId("SYS_A13")  # "Node supertags(s)"
URL_KEY_ID = NodeId("SYS_A78")  # "URL"
CHECKBOX_KEY_ID = NodeId("SYS_A55")  # Checkbox attribute
LANGUAGE_KEY_ID = NodeId("SYS_A70")  # Code language attribute
MEDIA_KEY_ID = NodeId("SYS_T15")  # Media/image URL
MIME_TYPE_KEY_ID = NodeId("SYS_T157")  # MIME type of media

# Common numeric thresholds
MIN_TUPLE_CHILDREN = 2

# Search-related IDs
SEARCH_EXPRESSION_KEY_ID = NodeId("SYS_A15")  # Search expression attribute
AND_OPERATOR_ID = NodeId("SYS_A41")  # AND operator
OR_OPERATOR_ID = NodeId("SYS_A42")  # OR operator
NOT_OPERATOR_ID = NodeId("SYS_A43")  # NOT operator

# System type IDs
EVENT_TYPE_ID = NodeId("SYS_T103")  # Event type
MEETING_TYPE_ID = NodeId("SYS_T98")  # Meeting type

# Value IDs
CHECKBOX_CHECKED_ID = NodeId("SYS_V03")  # Checked checkbox value
CHECKBOX_UNCHECKED_ID = NodeId("SYS_V04")  # Unchecked checkbox value
