#!/bin/sh
set -eu
kubectl -n gitlab-spike wait --for=condition=Ready pod -l app=toolbox,release=gitlab-spike --timeout=600s
pod=$(kubectl -n gitlab-spike get pods -l app=toolbox,release=gitlab-spike -o jsonpath='{.items[0].metadata.name}')
kubectl -n gitlab-spike exec "$pod" -c toolbox -- gitlab-rails runner /opt/spike-bootstrap/main.rb
