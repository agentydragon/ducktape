# Archived InvenTree token provisioner

This is the historical source for the InvenTree API-token Job and CronJob. The
active InvenTree deployment has been decommissioned, so this package is no longer
included in the CI image-publishing inventory.

The parked Kubernetes declarations remain under
`cluster/k8s/inventree/token-provisioner/` as revival inputs. Reviving InvenTree
requires restoring the image target and Flux ImageRepository/ImagePolicy before
unsuspending the token-provisioner Kustomization.
