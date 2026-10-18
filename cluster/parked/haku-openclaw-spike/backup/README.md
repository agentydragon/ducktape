# Retained Restic recovery package

This directory preserves recovery metadata for the retired Haku OpenClaw spike.
It has no Flux owner and is not applied. The external SeaweedFS bucket and its
historical Restic snapshots were not modified when the app namespace and PVCs
were retired.

Keep `repository.sops.yaml` unchanged: it contains the original encrypted Restic
repository password. The generated manifests preserve the bucket name, Restic
repository and S3 credential Secret name, with `Retain` policies for both the
bucket and generated S3 credentials.

Recovery requires creating a replacement namespace and PVC, restoring the
SeaweedFS cross-namespace `ResourceReferenceGrant`, and minting S3 credentials
again before creating a Restic restore. The archived backup Kustomization is not
ready to apply by itself.
