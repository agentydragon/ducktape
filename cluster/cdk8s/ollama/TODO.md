# TODO — local inference provisioning

- Make TabbyAPI checkpoint staging declarative. Pin the upstream revision and file
  digests for each checkpoint, including the abliterated build. Resume incomplete
  downloads and publish each verified checkpoint to its own directory before
  GitOps points the serving workload at it. Keep the previous checkpoint available
  for rollback. See the [current staging procedure](README.md#tabbyapi-checkpoint-staging).
