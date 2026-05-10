FROM ubuntu:24.04

ENV VPN_INTERFACE=wg0 \
    VPN_EXPECTED_IPV4=10.2.0.2 \
    TRANSMISSION_CONFIG_DIR=/config \
    NATPMP_RENEW_INTERVAL_SECONDS=45

VOLUME /data
VOLUME /config

COPY transmission-default-settings.json /usr/local/share/transmission/default-settings.json
COPY scripts/transmission_with_natpmp.py /usr/local/bin/transmission_with_natpmp.py

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    ca-certificates dumb-init transmission-daemon python3 natpmpc iproute2 \
    && rm -rf /tmp/* /var/tmp/* /var/lib/apt/lists/* \
    && useradd -u 911 -U -d /config -s /bin/false abc \
    && usermod -a -G users abc \
    && chmod 755 /usr/local/bin/transmission_with_natpmp.py

EXPOSE 9091/tcp 51413/tcp 51413/udp

USER abc

ENTRYPOINT ["dumb-init", "--", "python3", "/usr/local/bin/transmission_with_natpmp.py"]