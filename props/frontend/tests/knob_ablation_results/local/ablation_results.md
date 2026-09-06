# Knob Ablation Results

Execution environment: local

Each row shows what happens when that knob is **removed** from the baseline.

| Knob                                   | Category    | DefinitionDetail                                          | DistributionChartRecall                                   |
| -------------------------------------- | ----------- | --------------------------------------------------------- | --------------------------------------------------------- |
| flag_disable_gpu                       | chrome_flag | ERROR: Protocol error (Target.setDiscoverTargets): Target | ERROR: Protocol error (Target.setDiscoverTargets): Target |
| flag_font_render_hinting               | chrome_flag | IDENTICAL                                                 | 701px (0.226%)                                            |
| flag_disable_font_subpixel_positioning | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_lcd_text                  | chrome_flag | 3776px (0.597%)                                           | 178px (0.057%)                                            |
| flag_force_color_profile               | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_accelerated_2d_canvas     | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_gpu_compositing           | chrome_flag | ERROR: Network.enable timed out. Increase the 'protocolTi | ERROR: Network.enable timed out. Increase the 'protocolTi |
| flag_disable_software_rasterizer       | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_skia_runtime_opts         | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_partial_raster            | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_backing_store_limit       | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_use_gl                            | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_force_device_scale_factor         | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_features                  | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_accelerated_video_decode  | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_canvas_aa                 | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_2d_canvas_clip_aa         | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_webgl                     | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_webgl2                    | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_blink_settings                    | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_smooth_scrolling          | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_threaded_animation        | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_threaded_scrolling        | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| flag_disable_checker_imaging           | chrome_flag | IDENTICAL                                                 | IDENTICAL                                                 |
| css_font_smoothing                     | css         | IDENTICAL                                                 | IDENTICAL                                                 |
| css_text_rendering                     | css         | IDENTICAL                                                 | IDENTICAL                                                 |
| css_animation_disable                  | css         | IDENTICAL                                                 | IDENTICAL                                                 |
| css_hermetic_font                      | css         | SIZE MISMATCH                                             | 1124px (0.363%)                                           |
| env_fontconfig                         | env         | IDENTICAL                                                 | IDENTICAL                                                 |
| env_freetype                           | env         | IDENTICAL                                                 | IDENTICAL                                                 |
| media_color_scheme                     | media       | IDENTICAL                                                 | IDENTICAL                                                 |
| media_reduced_motion                   | media       | IDENTICAL                                                 | IDENTICAL                                                 |
| viewport_scale_factor                  | viewport    | SIZE MISMATCH                                             | SIZE MISMATCH                                             |
