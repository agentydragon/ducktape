#!/usr/bin/env python3
import argparse
from textwrap import dedent

STORAGE_CLASSES = ("local-path-ovh-ssd", "local-path-ovh-hdd", "seaweedfs-ovh-ssd", "seaweedfs-ovh")


def slug(value: str) -> str:
    return value.replace("_", "-").replace(".", "-")


def render(storage_class: str, repeat: int, run_id: str, pvc_size: str) -> str:
    name = f"sqlite-bench-{slug(storage_class)}-{repeat}"
    bench_id = f"{run_id}-{storage_class}-{repeat}"
    return dedent(
        f"""\
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: {name}
          namespace: sqlite-storage-bench
          labels:
            app.kubernetes.io/name: sqlite-storage-bench
            sqlite-storage-bench/run-id: {run_id}
            sqlite-storage-bench/storage-class: {storage_class}
            sqlite-storage-bench/repeat: "{repeat}"
        spec:
          accessModes:
            - ReadWriteOnce
          storageClassName: {storage_class}
          resources:
            requests:
              storage: {pvc_size}
        ---
        apiVersion: batch/v1
        kind: Job
        metadata:
          name: {name}
          namespace: sqlite-storage-bench
          labels:
            app.kubernetes.io/name: sqlite-storage-bench
            sqlite-storage-bench/run-id: {run_id}
            sqlite-storage-bench/storage-class: {storage_class}
            sqlite-storage-bench/repeat: "{repeat}"
        spec:
          backoffLimit: 0
          activeDeadlineSeconds: 7200
          template:
            metadata:
              labels:
                app.kubernetes.io/name: sqlite-storage-bench
                sqlite-storage-bench/run-id: {run_id}
                sqlite-storage-bench/storage-class: {storage_class}
                sqlite-storage-bench/repeat: "{repeat}"
            spec:
              restartPolicy: Never
              securityContext:
                runAsNonRoot: true
                runAsUser: 1000
                runAsGroup: 1000
                fsGroup: 1000
                fsGroupChangePolicy: OnRootMismatch
                seccompProfile:
                  type: RuntimeDefault
              nodeSelector:
                topology.kubernetes.io/zone: hil-ovh
              containers:
                - name: sqlite-bench
                  image: python:3.13-slim
                  imagePullPolicy: IfNotPresent
                  command:
                    - python3
                    - /bench/bench_sqlite_storage.py
                  env:
                    - name: BENCH_ID
                      value: {bench_id}
                    - name: STORAGE_CLASS
                      value: {storage_class}
                    - name: REPEAT_INDEX
                      value: "{repeat}"
                    - name: POD_NAME
                      valueFrom:
                        fieldRef:
                          fieldPath: metadata.name
                    - name: NODE_NAME
                      valueFrom:
                        fieldRef:
                          fieldPath: spec.nodeName
                  resources:
                    requests:
                      cpu: "1"
                      memory: 1Gi
                    limits:
                      cpu: "2"
                      memory: 2Gi
                  securityContext:
                    allowPrivilegeEscalation: false
                    capabilities:
                      drop:
                        - ALL
                  volumeMounts:
                    - name: bench-script
                      mountPath: /bench
                      readOnly: true
                    - name: data
                      mountPath: /data
              volumes:
                - name: bench-script
                  configMap:
                    name: sqlite-storage-bench-script
                    defaultMode: 0555
                - name: data
                  persistentVolumeClaim:
                    claimName: {name}
        """
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-class", required=True, choices=STORAGE_CLASSES)
    parser.add_argument("--repeat", required=True, type=int)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--pvc-size", default="8Gi")
    args = parser.parse_args()
    print(render(args.storage_class, args.repeat, args.run_id, args.pvc_size))


if __name__ == "__main__":
    main()
