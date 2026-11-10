# Pull the amd64 child selected by the deployed Ubuntu 24.04 index
# sha256:ea164c514b308286bdc179b403298efd2d8ae549af518a82a029e1bbb027f3a4.
{ pkgs }:
pkgs.dockerTools.pullImage {
  imageName = "quay.io/containerdisks/ubuntu";
  imageDigest = "sha256:2b5ff51129962719239c359bf721bfe99757cb49884447f1b49b2e585d9e6ddf";
  hash = "sha256-3m2kXEU3QHhI/hEajYYnb9uW+pyOTHuog6IRg0BJLSQ=";
  finalImageName = "quay.io/containerdisks/ubuntu";
  finalImageTag = "24.04";
  arch = "amd64";
}
