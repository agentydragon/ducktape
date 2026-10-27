from util.oci import OciImage

IMAGE = OciImage(
    "_main/agentplane/app/frontend/serving/static_test_image.rloc",
    "agentplane-frontend:test",
    "_main/agentplane/app/frontend/serving/static_test_image_ci_load.sh",
)
