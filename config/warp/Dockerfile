# Keep MicroWARP's registration, WireGuard setup and policy routing intact.
ARG MICROWARP_IMAGE=ghcr.io/ccbkkb/microwarp@sha256:d5b709efd49dbec3a965c617eb263b46cdf1449e65776d1b9a351927439b2b17
FROM ${MICROWARP_IMAGE} AS hev-builder

# Hev SOCKS5 Server 2.13.1; its submodules are pinned by this commit.
ARG HEV_REF=404d8c3dfeedb561cc6135ee20a6ddbde74c3481
RUN apk add --no-cache build-base git \
    && git init /src \
    && git -C /src remote add origin https://github.com/heiher/hev-socks5-server.git \
    && git -C /src fetch --depth 1 origin "${HEV_REF}" \
    && git -C /src checkout --detach FETCH_HEAD \
    && git -C /src submodule update --init --recursive --depth 1 \
    && make -C /src -j1 ENABLE_STATIC=1

FROM ${MICROWARP_IMAGE}
COPY --from=hev-builder /src/bin/hev-socks5-server /usr/local/bin/hev-socks5-server
RUN mv /usr/local/bin/microsocks /usr/local/bin/microsocks-original
COPY microsocks-compat.sh /usr/local/bin/microsocks
RUN chmod 755 /usr/local/bin/microsocks
LABEL org.opencontainers.image.title="MicroWARP with Hev SOCKS5 TCP/UDP" \
      io.xui.microwarp.base-revision="82a4c62042fb68030bb9e109d16bf8c17bd7e117" \
      io.xui.hev.revision="404d8c3dfeedb561cc6135ee20a6ddbde74c3481"
