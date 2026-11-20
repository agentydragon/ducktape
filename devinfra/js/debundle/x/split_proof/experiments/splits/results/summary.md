| Bundle    | Strategy        | Splits | Accepted | Accepted, Node differs | Rejected, Node breaks | Rejected, Node fine | Rejected splits that fail each check             |
| --------- | --------------- | ------ | -------- | ---------------------- | --------------------- | ------------------- | ------------------------------------------------ |
| acorn.min | blocks          | 7      | 5        | 0                      | 1                     | 1                   | OrderOk 2                                        |
| acorn.min | cut             | 373    | 371      | 0                      | 1                     | 1                   | AssignsHome 2                                    |
| acorn.min | cut_entry_first | 37     | 0        | 0                      | 37                    | 0                   | ExportOutsideEntry 37, OrderOk 37                |
| acorn.min | halves          | 1      | 1        | 0                      | 0                     | 0                   |                                                  |
| acorn.min | random          | 80     | 0        | 0                      | 80                    | 0                   | OrderOk 80, AssignsHome 73                       |
| acorn     | blocks          | 7      | 5        | 0                      | 2                     | 0                   | AssignsHome 2, OrderOk 2                         |
| acorn     | cut             | 446    | 442      | 0                      | 3                     | 1                   | AssignsHome 4                                    |
| acorn     | cut_entry_first | 44     | 0        | 0                      | 44                    | 0                   | ExportOutsideEntry 44, OrderOk 44, AssignsHome 1 |
| acorn     | halves          | 1      | 1        | 0                      | 0                     | 0                   |                                                  |
| acorn     | random          | 80     | 0        | 0                      | 80                    | 0                   | AssignsHome 80, OrderOk 80, ExportNames 77       |
| lodash    | blocks          | 7      | 7        | 0                      | 0                     | 0                   |                                                  |
| lodash    | cut             | 669    | 668      | 0                      | 0                     | 1                   | AssignsHome 1                                    |
| lodash    | cut_entry_first | 66     | 0        | 0                      | 66                    | 0                   | ExportOutsideEntry 66, OrderOk 66                |
| lodash    | halves          | 1      | 1        | 0                      | 0                     | 0                   |                                                  |
| lodash    | random          | 80     | 0        | 0                      | 80                    | 0                   | OrderOk 80, ExportNames 66, AssignsHome 58       |
| semver    | blocks          | 7      | 7        | 0                      | 0                     | 0                   |                                                  |
| semver    | cut             | 58     | 58       | 0                      | 0                     | 0                   |                                                  |
| semver    | cut_entry_first | 5      | 0        | 0                      | 5                     | 0                   | ExportOutsideEntry 5, OrderOk 5                  |
| semver    | halves          | 1      | 1        | 0                      | 0                     | 0                   |                                                  |
| semver    | random          | 80     | 2        | 0                      | 76                    | 2                   | OrderOk 78                                       |
| zod.min   | blocks          | 7      | 1        | 0                      | 5                     | 1                   | OrderOk 6                                        |
| zod.min   | cut             | 201    | 199      | 0                      | 2                     | 0                   | AssignsHome 2                                    |
| zod.min   | cut_entry_first | 20     | 0        | 0                      | 20                    | 0                   | ExportOutsideEntry 20, OrderOk 20                |
| zod.min   | halves          | 1      | 1        | 0                      | 0                     | 0                   |                                                  |
| zod.min   | random          | 80     | 0        | 0                      | 80                    | 0                   | OrderOk 80, AssignsHome 79                       |
| zod       | blocks          | 7      | 1        | 0                      | 6                     | 0                   | OrderOk 6                                        |
| zod       | cut             | 267    | 263      | 0                      | 4                     | 0                   | AssignsHome 4                                    |
| zod       | cut_entry_first | 26     | 0        | 0                      | 26                    | 0                   | ExportOutsideEntry 26, OrderOk 26                |
| zod       | halves          | 1      | 1        | 0                      | 0                     | 0                   |                                                  |
| zod       | random          | 80     | 0        | 0                      | 80                    | 0                   | AssignsHome 80, OrderOk 80, ExportNames 15       |
