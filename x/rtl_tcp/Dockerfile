# RTL-SDR Blog V4 requires the rtl-sdr-blog fork — Debian's stock librtlsdr
# doesn't support the R828D tuner (fails with "PLL not locked" / i2c errors).
FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git pkg-config libusb-1.0-0-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/rtlsdrblog/rtl-sdr-blog.git /src \
    && cd /src && mkdir build && cd build \
    && cmake .. -DCMAKE_INSTALL_PREFIX=/usr -DDETACH_KERNEL_DRIVER=ON \
    && make -j$(nproc) && make install

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/bin/rtl_tcp /usr/bin/rtl_tcp
COPY --from=build /usr/lib/x86_64-linux-gnu/librtlsdr.so* /usr/lib/x86_64-linux-gnu/
RUN ldconfig
ENTRYPOINT ["rtl_tcp", "-a", "0.0.0.0"]
