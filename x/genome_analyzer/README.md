# Genome analysis: existing tools

Retired experiment: annotate a MyHeritage raw-DNA CSV by rsID using
MyVariant.info (ClinVar, gnomAD and CADD), then generate HTML reports. This
duplicated existing annotation and reporting tools; it did not process WGS
sequencing reads.

Use an existing project for further work:

| Task                                                                                                                      | Open source project                                                                                                               | Software license |
| ------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| WGS read processing, germline variant calling and annotation                                                              | [nf-core/Sarek](https://github.com/nf-core/sarek), with [input and stage documentation](https://nf-co.re/sarek/latest/docs/usage) | MIT              |
| Functional consequence annotation of called variants, including offline VCF processing                                    | [Ensembl Variant Effect Predictor (VEP)](https://github.com/Ensembl/ensembl-vep)                                                  | Apache-2.0       |
| Local variant annotation, filtering, interactive exploration and report exports; modules include ClinVar, gnomAD and CADD | [OpenCRAVAT](https://docs.opencravat.org/en/latest/), [source](https://github.com/KarchinLab/open-cravat)                         | MIT              |

Choose the stage that matches the input: sequencing reads require processing and
variant calling; an existing VCF belongs at the annotation and filtering stage.

The software licenses above do not cover every optional tool or annotation
dataset; [OpenCRAVAT documents dataset licensing separately](https://www.opencravat.org/faq/).
