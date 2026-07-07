# SQLite storage benchmark summary (20260707T002921Z)

- Runs parsed: 20
- Result rows: 200

## Nodes

- local-path-ovh-hdd repeat 1: ovh-ns103656
- local-path-ovh-hdd repeat 2: ovh-ns103656
- local-path-ovh-hdd repeat 3: ovh-ns103656
- local-path-ovh-hdd repeat 4: ovh-ns103656
- local-path-ovh-hdd repeat 5: ovh-ns103656
- local-path-ovh-ssd repeat 1: ovh-ns104963
- local-path-ovh-ssd repeat 2: ovh-ns104963
- local-path-ovh-ssd repeat 3: ovh-ns104963
- local-path-ovh-ssd repeat 4: ovh-ns104963
- local-path-ovh-ssd repeat 5: ovh-ns104952
- seaweedfs-ovh repeat 1: ovh-ns103656
- seaweedfs-ovh repeat 2: ovh-ns103656
- seaweedfs-ovh repeat 3: ovh-ns103656
- seaweedfs-ovh repeat 4: ovh-ns103656
- seaweedfs-ovh repeat 5: ovh-ns103656
- seaweedfs-ovh-ssd repeat 1: ovh-ns103656
- seaweedfs-ovh-ssd repeat 2: ovh-ns103656
- seaweedfs-ovh-ssd repeat 3: ovh-ns103656
- seaweedfs-ovh-ssd repeat 4: ovh-ns103656
- seaweedfs-ovh-ssd repeat 5: ovh-ns103656

## Aggregated metrics

| StorageClass         | Workload                   | Metric                  |        p50 |        p95 |        max |
| -------------------- | -------------------------- | ----------------------- | ---------: | ---------: | ---------: |
| `local-path-ovh-hdd` | `activitywatch_batch_10`   | `inserts_per_sec`       |    900.243 |    903.444 |    906.203 |
| `local-path-ovh-hdd` | `activitywatch_batch_10`   | `max_ms`                |    271.049 |    320.730 |    332.598 |
| `local-path-ovh-hdd` | `activitywatch_batch_10`   | `mean_ms`               |     11.108 |     11.118 |     11.660 |
| `local-path-ovh-hdd` | `activitywatch_batch_10`   | `p50_ms`                |      8.591 |      8.598 |      8.645 |
| `local-path-ovh-hdd` | `activitywatch_batch_10`   | `p95_ms`                |     23.042 |     24.931 |     24.976 |
| `local-path-ovh-hdd` | `activitywatch_batch_100`  | `inserts_per_sec`       |   5731.977 |   5745.762 |   5759.335 |
| `local-path-ovh-hdd` | `activitywatch_batch_100`  | `max_ms`                |    211.165 |    239.794 |    268.403 |
| `local-path-ovh-hdd` | `activitywatch_batch_100`  | `mean_ms`               |     17.446 |     18.896 |     19.087 |
| `local-path-ovh-hdd` | `activitywatch_batch_100`  | `p50_ms`                |      9.313 |      9.327 |      9.551 |
| `local-path-ovh-hdd` | `activitywatch_batch_100`  | `p95_ms`                |     90.112 |     92.154 |    102.216 |
| `local-path-ovh-hdd` | `activitywatch_batch_1000` | `inserts_per_sec`       |  29002.391 |  31571.732 |  33100.028 |
| `local-path-ovh-hdd` | `activitywatch_batch_1000` | `max_ms`                |    188.067 |    203.621 |    214.637 |
| `local-path-ovh-hdd` | `activitywatch_batch_1000` | `mean_ms`               |     34.480 |     34.986 |     39.299 |
| `local-path-ovh-hdd` | `activitywatch_batch_1000` | `p50_ms`                |     18.032 |     19.976 |     24.171 |
| `local-path-ovh-hdd` | `activitywatch_batch_1000` | `p95_ms`                |    127.983 |    137.346 |    144.344 |
| `local-path-ovh-hdd` | `autocommit_1000`          | `max_ms`                |    197.700 |    316.760 |    324.942 |
| `local-path-ovh-hdd` | `autocommit_1000`          | `mean_ms`               |     26.093 |     26.503 |     31.169 |
| `local-path-ovh-hdd` | `autocommit_1000`          | `p50_ms`                |     25.019 |     25.020 |     33.286 |
| `local-path-ovh-hdd` | `autocommit_1000`          | `p95_ms`                |     33.296 |     33.681 |     41.705 |
| `local-path-ovh-hdd` | `autocommit_10000`         | `max_ms`                |    213.825 |    227.291 |    279.919 |
| `local-path-ovh-hdd` | `autocommit_10000`         | `mean_ms`               |     10.603 |     10.653 |     10.781 |
| `local-path-ovh-hdd` | `autocommit_10000`         | `p50_ms`                |      8.360 |      8.362 |      8.367 |
| `local-path-ovh-hdd` | `autocommit_10000`         | `p95_ms`                |     25.030 |     25.053 |     26.680 |
| `local-path-ovh-hdd` | `grocy_batch_100`          | `inserts_per_sec`       |   5367.634 |   5524.626 |   5543.816 |
| `local-path-ovh-hdd` | `grocy_batch_100`          | `query_max_ms`          |      0.135 |      0.142 |      0.181 |
| `local-path-ovh-hdd` | `grocy_batch_100`          | `query_p50_ms`          |      0.021 |      0.026 |      0.036 |
| `local-path-ovh-hdd` | `grocy_batch_100`          | `query_p95_ms`          |      0.041 |      0.047 |      0.048 |
| `local-path-ovh-hdd` | `grocy_batch_100`          | `write_max_ms`          |    684.790 |    710.876 |    736.201 |
| `local-path-ovh-hdd` | `grocy_batch_100`          | `write_p50_ms`          |      8.350 |      8.414 |      8.480 |
| `local-path-ovh-hdd` | `grocy_batch_100`          | `write_p95_ms`          |     24.663 |     32.272 |     32.960 |
| `local-path-ovh-hdd` | `queries_10000`            | `bucket_count_max_ms`   |      0.054 |      0.057 |      0.059 |
| `local-path-ovh-hdd` | `queries_10000`            | `bucket_count_p50_ms`   |      0.019 |      0.020 |      0.020 |
| `local-path-ovh-hdd` | `queries_10000`            | `bucket_count_p95_ms`   |      0.020 |      0.020 |      0.021 |
| `local-path-ovh-hdd` | `queries_10000`            | `close_reopen_count_ms` |      0.577 |      0.581 |      0.658 |
| `local-path-ovh-hdd` | `queries_10000`            | `time_range_max_ms`     |      1.043 |      1.280 |      1.901 |
| `local-path-ovh-hdd` | `queries_10000`            | `time_range_p50_ms`     |      0.291 |      0.375 |      0.443 |
| `local-path-ovh-hdd` | `queries_10000`            | `time_range_p95_ms`     |      0.608 |      1.262 |      1.648 |
| `local-path-ovh-hdd` | `queries_100000`           | `bucket_count_max_ms`   |      0.227 |      0.243 |      0.582 |
| `local-path-ovh-hdd` | `queries_100000`           | `bucket_count_p50_ms`   |      0.148 |      0.148 |      0.148 |
| `local-path-ovh-hdd` | `queries_100000`           | `bucket_count_p95_ms`   |      0.182 |      0.190 |      0.530 |
| `local-path-ovh-hdd` | `queries_100000`           | `close_reopen_count_ms` |      3.083 |      3.971 |      4.770 |
| `local-path-ovh-hdd` | `queries_100000`           | `time_range_max_ms`     |     19.183 |     20.517 |     23.402 |
| `local-path-ovh-hdd` | `queries_100000`           | `time_range_p50_ms`     |      3.988 |      4.122 |      7.312 |
| `local-path-ovh-hdd` | `queries_100000`           | `time_range_p95_ms`     |      9.253 |     10.256 |     11.419 |
| `local-path-ovh-hdd` | `queries_1000000`          | `bucket_count_max_ms`   |      5.471 |      5.493 |     12.870 |
| `local-path-ovh-hdd` | `queries_1000000`          | `bucket_count_p50_ms`   |      1.486 |      1.526 |      1.770 |
| `local-path-ovh-hdd` | `queries_1000000`          | `bucket_count_p95_ms`   |      3.894 |      4.134 |      5.292 |
| `local-path-ovh-hdd` | `queries_1000000`          | `close_reopen_count_ms` |     10.620 |     26.728 |     27.131 |
| `local-path-ovh-hdd` | `queries_1000000`          | `time_range_max_ms`     |     15.170 |     17.887 |     20.468 |
| `local-path-ovh-hdd` | `queries_1000000`          | `time_range_p50_ms`     |      3.780 |      3.817 |      3.834 |
| `local-path-ovh-hdd` | `queries_1000000`          | `time_range_p95_ms`     |      9.280 |     10.076 |     14.960 |
| `local-path-ovh-hdd` | `wal_checkpoint_truncate`  | `checkpoint_ms`         |    119.217 |    122.089 |    124.743 |
| `local-path-ovh-hdd` | `wal_checkpoint_truncate`  | `close_ms`              |      0.591 |      0.706 |      1.448 |
| `local-path-ovh-hdd` | `wal_checkpoint_truncate`  | `reopen_count_ms`       |      2.038 |      2.778 |      6.527 |
| `local-path-ovh-ssd` | `activitywatch_batch_10`   | `inserts_per_sec`       |  44876.808 |  45088.369 |  47993.986 |
| `local-path-ovh-ssd` | `activitywatch_batch_10`   | `max_ms`                |      6.517 |      7.670 |      9.875 |
| `local-path-ovh-ssd` | `activitywatch_batch_10`   | `mean_ms`               |      0.223 |      0.223 |      0.224 |
| `local-path-ovh-ssd` | `activitywatch_batch_10`   | `p50_ms`                |      0.171 |      0.173 |      0.177 |
| `local-path-ovh-ssd` | `activitywatch_batch_10`   | `p95_ms`                |      0.274 |      0.274 |      0.277 |
| `local-path-ovh-ssd` | `activitywatch_batch_100`  | `inserts_per_sec`       | 148002.858 | 148684.281 | 155505.864 |
| `local-path-ovh-ssd` | `activitywatch_batch_100`  | `max_ms`                |      9.773 |     11.456 |     13.138 |
| `local-path-ovh-ssd` | `activitywatch_batch_100`  | `mean_ms`               |      0.676 |      0.693 |      0.746 |
| `local-path-ovh-ssd` | `activitywatch_batch_100`  | `p50_ms`                |      0.532 |      0.533 |      0.636 |
| `local-path-ovh-ssd` | `activitywatch_batch_100`  | `p95_ms`                |      1.500 |      1.517 |      1.521 |
| `local-path-ovh-ssd` | `activitywatch_batch_1000` | `inserts_per_sec`       | 320225.794 | 320260.305 | 330016.240 |
| `local-path-ovh-ssd` | `activitywatch_batch_1000` | `max_ms`                |     16.554 |     18.182 |     20.297 |
| `local-path-ovh-ssd` | `activitywatch_batch_1000` | `mean_ms`               |      3.123 |      3.213 |      3.550 |
| `local-path-ovh-ssd` | `activitywatch_batch_1000` | `p50_ms`                |      2.384 |      2.530 |      3.221 |
| `local-path-ovh-ssd` | `activitywatch_batch_1000` | `p95_ms`                |      5.342 |      5.692 |      6.090 |
| `local-path-ovh-ssd` | `autocommit_1000`          | `max_ms`                |      7.040 |      7.631 |      8.033 |
| `local-path-ovh-ssd` | `autocommit_1000`          | `mean_ms`               |      1.230 |      1.407 |      1.456 |
| `local-path-ovh-ssd` | `autocommit_1000`          | `p50_ms`                |      1.101 |      1.477 |      1.573 |
| `local-path-ovh-ssd` | `autocommit_1000`          | `p95_ms`                |      2.013 |      2.027 |      2.033 |
| `local-path-ovh-ssd` | `autocommit_10000`         | `max_ms`                |     10.072 |     11.313 |     11.755 |
| `local-path-ovh-ssd` | `autocommit_10000`         | `mean_ms`               |      0.261 |      0.273 |      0.277 |
| `local-path-ovh-ssd` | `autocommit_10000`         | `p50_ms`                |      0.093 |      0.094 |      0.096 |
| `local-path-ovh-ssd` | `autocommit_10000`         | `p95_ms`                |      1.233 |      1.586 |      1.655 |
| `local-path-ovh-ssd` | `grocy_batch_100`          | `inserts_per_sec`       | 170102.830 | 171038.622 | 172806.612 |
| `local-path-ovh-ssd` | `grocy_batch_100`          | `query_max_ms`          |      0.124 |      0.136 |      1.507 |
| `local-path-ovh-ssd` | `grocy_batch_100`          | `query_p50_ms`          |      0.017 |      0.018 |      0.032 |
| `local-path-ovh-ssd` | `grocy_batch_100`          | `query_p95_ms`          |      0.040 |      0.040 |      0.589 |
| `local-path-ovh-ssd` | `grocy_batch_100`          | `write_max_ms`          |      9.605 |     11.636 |     19.709 |
| `local-path-ovh-ssd` | `grocy_batch_100`          | `write_p50_ms`          |      0.389 |      0.393 |      0.529 |
| `local-path-ovh-ssd` | `grocy_batch_100`          | `write_p95_ms`          |      1.166 |      1.370 |      1.391 |
| `local-path-ovh-ssd` | `queries_10000`            | `bucket_count_max_ms`   |      0.077 |      0.098 |      0.108 |
| `local-path-ovh-ssd` | `queries_10000`            | `bucket_count_p50_ms`   |      0.018 |      0.056 |      0.056 |
| `local-path-ovh-ssd` | `queries_10000`            | `bucket_count_p95_ms`   |      0.052 |      0.057 |      0.058 |
| `local-path-ovh-ssd` | `queries_10000`            | `close_reopen_count_ms` |      0.446 |      0.459 |      0.470 |
| `local-path-ovh-ssd` | `queries_10000`            | `time_range_max_ms`     |      1.184 |      1.186 |      1.191 |
| `local-path-ovh-ssd` | `queries_10000`            | `time_range_p50_ms`     |      0.264 |      0.357 |      0.509 |
| `local-path-ovh-ssd` | `queries_10000`            | `time_range_p95_ms`     |      1.078 |      1.107 |      1.112 |
| `local-path-ovh-ssd` | `queries_100000`           | `bucket_count_max_ms`   |      0.467 |      0.520 |      0.544 |
| `local-path-ovh-ssd` | `queries_100000`           | `bucket_count_p50_ms`   |      0.138 |      0.355 |      0.491 |
| `local-path-ovh-ssd` | `queries_100000`           | `bucket_count_p95_ms`   |      0.443 |      0.502 |      0.505 |
| `local-path-ovh-ssd` | `queries_100000`           | `close_reopen_count_ms` |      0.939 |      1.085 |      1.184 |
| `local-path-ovh-ssd` | `queries_100000`           | `time_range_max_ms`     |     11.987 |     18.493 |     20.762 |
| `local-path-ovh-ssd` | `queries_100000`           | `time_range_p50_ms`     |      4.659 |      5.711 |      6.022 |
| `local-path-ovh-ssd` | `queries_100000`           | `time_range_p95_ms`     |      9.098 |     10.018 |     11.010 |
| `local-path-ovh-ssd` | `queries_1000000`          | `bucket_count_max_ms`   |      4.914 |      5.038 |     16.616 |
| `local-path-ovh-ssd` | `queries_1000000`          | `bucket_count_p50_ms`   |      1.557 |      1.851 |      2.040 |
| `local-path-ovh-ssd` | `queries_1000000`          | `bucket_count_p95_ms`   |      4.538 |      4.879 |      4.884 |
| `local-path-ovh-ssd` | `queries_1000000`          | `close_reopen_count_ms` |      6.154 |      6.542 |      7.200 |
| `local-path-ovh-ssd` | `queries_1000000`          | `time_range_max_ms`     |     12.150 |     23.081 |     23.935 |
| `local-path-ovh-ssd` | `queries_1000000`          | `time_range_p50_ms`     |      4.389 |      4.495 |      6.676 |
| `local-path-ovh-ssd` | `queries_1000000`          | `time_range_p95_ms`     |     10.508 |     10.554 |     11.696 |
| `local-path-ovh-ssd` | `wal_checkpoint_truncate`  | `checkpoint_ms`         |      1.306 |      1.360 |      1.399 |
| `local-path-ovh-ssd` | `wal_checkpoint_truncate`  | `close_ms`              |      0.382 |      0.387 |      0.415 |
| `local-path-ovh-ssd` | `wal_checkpoint_truncate`  | `reopen_count_ms`       |      1.414 |      1.493 |      1.573 |
| `seaweedfs-ovh`      | `activitywatch_batch_10`   | `inserts_per_sec`       |    425.006 |    426.665 |    444.183 |
| `seaweedfs-ovh`      | `activitywatch_batch_10`   | `max_ms`                |    918.292 |   1029.405 |   1148.456 |
| `seaweedfs-ovh`      | `activitywatch_batch_10`   | `mean_ms`               |     23.529 |     24.289 |     25.183 |
| `seaweedfs-ovh`      | `activitywatch_batch_10`   | `p50_ms`                |     15.252 |     16.279 |     16.694 |
| `seaweedfs-ovh`      | `activitywatch_batch_10`   | `p95_ms`                |     39.062 |     39.201 |     39.322 |
| `seaweedfs-ovh`      | `activitywatch_batch_100`  | `inserts_per_sec`       |   1926.299 |   1989.539 |   2122.473 |
| `seaweedfs-ovh`      | `activitywatch_batch_100`  | `max_ms`                |    957.023 |   1046.414 |   1842.916 |
| `seaweedfs-ovh`      | `activitywatch_batch_100`  | `mean_ms`               |     51.913 |     56.995 |     58.218 |
| `seaweedfs-ovh`      | `activitywatch_batch_100`  | `p50_ms`                |     26.436 |     30.615 |     31.730 |
| `seaweedfs-ovh`      | `activitywatch_batch_100`  | `p95_ms`                |    270.289 |    280.115 |    326.932 |
| `seaweedfs-ovh`      | `activitywatch_batch_1000` | `inserts_per_sec`       |   9346.741 |  10225.217 |  13663.587 |
| `seaweedfs-ovh`      | `activitywatch_batch_1000` | `max_ms`                |    721.132 |    760.532 |   1096.420 |
| `seaweedfs-ovh`      | `activitywatch_batch_1000` | `mean_ms`               |    106.989 |    110.366 |    111.860 |
| `seaweedfs-ovh`      | `activitywatch_batch_1000` | `p50_ms`                |     50.390 |     54.034 |     56.880 |
| `seaweedfs-ovh`      | `activitywatch_batch_1000` | `p95_ms`                |    563.412 |    569.762 |    582.380 |
| `seaweedfs-ovh`      | `autocommit_1000`          | `max_ms`                |    256.321 |    281.642 |   2097.206 |
| `seaweedfs-ovh`      | `autocommit_1000`          | `mean_ms`               |     25.949 |     26.142 |     28.729 |
| `seaweedfs-ovh`      | `autocommit_1000`          | `p50_ms`                |     20.625 |     20.791 |     23.435 |
| `seaweedfs-ovh`      | `autocommit_1000`          | `p95_ms`                |     50.714 |     50.963 |     54.434 |
| `seaweedfs-ovh`      | `autocommit_10000`         | `max_ms`                |    504.234 |    577.762 |  21265.787 |
| `seaweedfs-ovh`      | `autocommit_10000`         | `mean_ms`               |     37.013 |     40.042 |     40.323 |
| `seaweedfs-ovh`      | `autocommit_10000`         | `p50_ms`                |     32.232 |     35.806 |     37.989 |
| `seaweedfs-ovh`      | `autocommit_10000`         | `p95_ms`                |     67.501 |     68.923 |     71.807 |
| `seaweedfs-ovh`      | `grocy_batch_100`          | `inserts_per_sec`       |   2886.681 |   3019.390 |   3626.394 |
| `seaweedfs-ovh`      | `grocy_batch_100`          | `query_max_ms`          |      0.302 |      0.496 |      6.009 |
| `seaweedfs-ovh`      | `grocy_batch_100`          | `query_p50_ms`          |      0.076 |      0.078 |      0.094 |
| `seaweedfs-ovh`      | `grocy_batch_100`          | `query_p95_ms`          |      0.125 |      0.138 |      0.153 |
| `seaweedfs-ovh`      | `grocy_batch_100`          | `write_max_ms`          |   1028.714 |   1053.703 |   1653.843 |
| `seaweedfs-ovh`      | `grocy_batch_100`          | `write_p50_ms`          |     18.392 |     18.583 |     18.607 |
| `seaweedfs-ovh`      | `grocy_batch_100`          | `write_p95_ms`          |     41.562 |     42.830 |     45.105 |
| `seaweedfs-ovh`      | `queries_10000`            | `bucket_count_max_ms`   |      0.167 |      0.169 |      0.189 |
| `seaweedfs-ovh`      | `queries_10000`            | `bucket_count_p50_ms`   |      0.087 |      0.088 |      0.089 |
| `seaweedfs-ovh`      | `queries_10000`            | `bucket_count_p95_ms`   |      0.138 |      0.146 |      0.157 |
| `seaweedfs-ovh`      | `queries_10000`            | `close_reopen_count_ms` |    632.040 |    739.330 |    758.520 |
| `seaweedfs-ovh`      | `queries_10000`            | `time_range_max_ms`     |      1.417 |      5.468 |      6.754 |
| `seaweedfs-ovh`      | `queries_10000`            | `time_range_p50_ms`     |      1.204 |      1.247 |      1.344 |
| `seaweedfs-ovh`      | `queries_10000`            | `time_range_p95_ms`     |      1.349 |      3.018 |      4.858 |
| `seaweedfs-ovh`      | `queries_100000`           | `bucket_count_max_ms`   |      7.016 |     22.704 |     23.778 |
| `seaweedfs-ovh`      | `queries_100000`           | `bucket_count_p50_ms`   |      1.415 |      2.567 |      3.044 |
| `seaweedfs-ovh`      | `queries_100000`           | `bucket_count_p95_ms`   |      5.951 |      6.782 |      7.965 |
| `seaweedfs-ovh`      | `queries_100000`           | `close_reopen_count_ms` |   1107.675 |   1264.955 |   1694.885 |
| `seaweedfs-ovh`      | `queries_100000`           | `time_range_max_ms`     |     24.736 |     27.448 |   1145.395 |
| `seaweedfs-ovh`      | `queries_100000`           | `time_range_p50_ms`     |      4.349 |      5.472 |     11.941 |
| `seaweedfs-ovh`      | `queries_100000`           | `time_range_p95_ms`     |     11.480 |     12.461 |     44.933 |
| `seaweedfs-ovh`      | `queries_1000000`          | `bucket_count_max_ms`   |     37.514 |    773.312 |    796.917 |
| `seaweedfs-ovh`      | `queries_1000000`          | `bucket_count_p50_ms`   |      7.830 |    476.793 |    511.533 |
| `seaweedfs-ovh`      | `queries_1000000`          | `bucket_count_p95_ms`   |     32.025 |    606.512 |    743.623 |
| `seaweedfs-ovh`      | `queries_1000000`          | `close_reopen_count_ms` |   8300.871 |   8457.847 |   8672.263 |
| `seaweedfs-ovh`      | `queries_1000000`          | `time_range_max_ms`     |     23.987 |    969.302 |   1055.161 |
| `seaweedfs-ovh`      | `queries_1000000`          | `time_range_p50_ms`     |      6.999 |     20.544 |     26.761 |
| `seaweedfs-ovh`      | `queries_1000000`          | `time_range_p95_ms`     |     12.768 |     71.225 |     72.554 |
| `seaweedfs-ovh`      | `wal_checkpoint_truncate`  | `checkpoint_ms`         |    430.475 |    438.011 |    448.293 |
| `seaweedfs-ovh`      | `wal_checkpoint_truncate`  | `close_ms`              |     22.350 |     23.356 |     30.789 |
| `seaweedfs-ovh`      | `wal_checkpoint_truncate`  | `reopen_count_ms`       |   1169.421 |   1417.403 |   1458.552 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_10`   | `inserts_per_sec`       |    456.530 |    456.990 |    458.926 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_10`   | `max_ms`                |   1140.916 |   1282.236 |   1373.574 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_10`   | `mean_ms`               |     21.904 |     21.944 |     23.022 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_10`   | `p50_ms`                |     13.354 |     13.485 |     14.354 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_10`   | `p95_ms`                |     35.122 |     37.530 |     38.317 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_100`  | `inserts_per_sec`       |   1993.017 |   2037.307 |   2064.541 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_100`  | `max_ms`                |    905.197 |   1208.884 |   1797.979 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_100`  | `mean_ms`               |     50.175 |     54.996 |     55.749 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_100`  | `p50_ms`                |     21.594 |     22.143 |     23.280 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_100`  | `p95_ms`                |    299.533 |    330.290 |    382.099 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_1000` | `inserts_per_sec`       |   9919.144 |  10228.440 |  10680.062 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_1000` | `max_ms`                |    814.067 |   1078.158 |   1152.456 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_1000` | `mean_ms`               |    100.815 |    113.505 |    118.420 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_1000` | `p50_ms`                |     42.205 |     42.257 |     46.459 |
| `seaweedfs-ovh-ssd`  | `activitywatch_batch_1000` | `p95_ms`                |    586.404 |    654.478 |    662.999 |
| `seaweedfs-ovh-ssd`  | `autocommit_1000`          | `max_ms`                |    121.785 |    139.539 |    266.915 |
| `seaweedfs-ovh-ssd`  | `autocommit_1000`          | `mean_ms`               |     23.404 |     25.095 |     25.180 |
| `seaweedfs-ovh-ssd`  | `autocommit_1000`          | `p50_ms`                |     20.579 |     22.237 |     22.849 |
| `seaweedfs-ovh-ssd`  | `autocommit_1000`          | `p95_ms`                |     49.720 |     50.223 |     50.312 |
| `seaweedfs-ovh-ssd`  | `autocommit_10000`         | `max_ms`                |    321.283 |    328.874 |   1005.271 |
| `seaweedfs-ovh-ssd`  | `autocommit_10000`         | `mean_ms`               |     39.946 |     40.272 |     41.166 |
| `seaweedfs-ovh-ssd`  | `autocommit_10000`         | `p50_ms`                |     32.555 |     37.886 |     39.010 |
| `seaweedfs-ovh-ssd`  | `autocommit_10000`         | `p95_ms`                |     65.640 |     67.288 |     74.326 |
| `seaweedfs-ovh-ssd`  | `grocy_batch_100`          | `inserts_per_sec`       |   2951.132 |   3037.491 |   3078.127 |
| `seaweedfs-ovh-ssd`  | `grocy_batch_100`          | `query_max_ms`          |      0.257 |      0.269 |      5.415 |
| `seaweedfs-ovh-ssd`  | `grocy_batch_100`          | `query_p50_ms`          |      0.065 |      0.065 |      0.071 |
| `seaweedfs-ovh-ssd`  | `grocy_batch_100`          | `query_p95_ms`          |      0.107 |      0.113 |      0.317 |
| `seaweedfs-ovh-ssd`  | `grocy_batch_100`          | `write_max_ms`          |   1472.864 |   1710.186 |   1907.060 |
| `seaweedfs-ovh-ssd`  | `grocy_batch_100`          | `write_p50_ms`          |     14.959 |     15.421 |     16.403 |
| `seaweedfs-ovh-ssd`  | `grocy_batch_100`          | `write_p95_ms`          |     41.766 |     44.107 |     47.681 |
| `seaweedfs-ovh-ssd`  | `queries_10000`            | `bucket_count_max_ms`   |      0.151 |      0.165 |      4.430 |
| `seaweedfs-ovh-ssd`  | `queries_10000`            | `bucket_count_p50_ms`   |      0.081 |      0.096 |      0.110 |
| `seaweedfs-ovh-ssd`  | `queries_10000`            | `bucket_count_p95_ms`   |      0.116 |      0.149 |      2.203 |
| `seaweedfs-ovh-ssd`  | `queries_10000`            | `close_reopen_count_ms` |    558.764 |    574.172 |    618.268 |
| `seaweedfs-ovh-ssd`  | `queries_10000`            | `time_range_max_ms`     |      1.413 |      1.781 |      2.021 |
| `seaweedfs-ovh-ssd`  | `queries_10000`            | `time_range_p50_ms`     |      0.373 |      0.422 |      0.514 |
| `seaweedfs-ovh-ssd`  | `queries_10000`            | `time_range_p95_ms`     |      0.927 |      1.272 |      1.278 |
| `seaweedfs-ovh-ssd`  | `queries_100000`           | `bucket_count_max_ms`   |     11.562 |     17.276 |     22.777 |
| `seaweedfs-ovh-ssd`  | `queries_100000`           | `bucket_count_p50_ms`   |      2.362 |      2.492 |      3.562 |
| `seaweedfs-ovh-ssd`  | `queries_100000`           | `bucket_count_p95_ms`   |      8.455 |      8.594 |      8.720 |
| `seaweedfs-ovh-ssd`  | `queries_100000`           | `close_reopen_count_ms` |   1185.616 |   1217.638 |   1367.007 |
| `seaweedfs-ovh-ssd`  | `queries_100000`           | `time_range_max_ms`     |     20.383 |     23.924 |     25.061 |
| `seaweedfs-ovh-ssd`  | `queries_100000`           | `time_range_p50_ms`     |      5.703 |      6.655 |      9.456 |
| `seaweedfs-ovh-ssd`  | `queries_100000`           | `time_range_p95_ms`     |     12.756 |     12.909 |     14.625 |
| `seaweedfs-ovh-ssd`  | `queries_1000000`          | `bucket_count_max_ms`   |     24.057 |     36.311 |    153.369 |
| `seaweedfs-ovh-ssd`  | `queries_1000000`          | `bucket_count_p50_ms`   |      6.215 |     14.834 |    111.875 |
| `seaweedfs-ovh-ssd`  | `queries_1000000`          | `bucket_count_p95_ms`   |     18.045 |     31.067 |    139.979 |
| `seaweedfs-ovh-ssd`  | `queries_1000000`          | `close_reopen_count_ms` |   6068.048 |   6155.507 |   6157.713 |
| `seaweedfs-ovh-ssd`  | `queries_1000000`          | `time_range_max_ms`     |     27.554 |    973.091 |   1060.958 |
| `seaweedfs-ovh-ssd`  | `queries_1000000`          | `time_range_p50_ms`     |      4.265 |     21.261 |     22.933 |
| `seaweedfs-ovh-ssd`  | `queries_1000000`          | `time_range_p95_ms`     |      7.664 |     58.413 |     71.441 |
| `seaweedfs-ovh-ssd`  | `wal_checkpoint_truncate`  | `checkpoint_ms`         |    453.853 |    459.291 |    677.214 |
| `seaweedfs-ovh-ssd`  | `wal_checkpoint_truncate`  | `close_ms`              |     21.322 |     29.540 |     36.658 |
| `seaweedfs-ovh-ssd`  | `wal_checkpoint_truncate`  | `reopen_count_ms`       |   1208.824 |   1393.253 |   1408.099 |
