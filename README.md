# What is this repo?

This repository builds a container image for running Transmission behind a WireGuard VPN sidecar while keeping a Proton-style NAT-PMP port mapping alive.

The image contains:

* Transmission daemon
* `natpmpc`
* A small Python supervisor that manages VPN health, NAT-PMP renewals, and Transmission lifecycle

# Runtime behavior

The container expects a separate WireGuard container in the same pod or network namespace. The supervisor does the following:

* Waits briefly for the configured VPN interface to appear at startup
* Exits the container if the VPN interface later disappears or no longer has the expected IPv4 address
* Seeds `/config/settings.json` from the bundled [transmission-default-settings.json](transmission-default-settings.json) if it does not already exist
* Forces Transmission to bind to the configured VPN IPv4 address
* Renews both UDP and TCP NAT-PMP mappings on a loop using the configured gateway
* Updates Transmission's peer port when the mapped public port changes
* Tries a Transmission reload first, then falls back to a graceful stop/start if the new port is not applied

If UDP and TCP NAT-PMP mappings ever return different public ports, the container uses the TCP port for Transmission and keeps retrying renewals.

If NAT-PMP renewal fails after startup, the container keeps retrying and leaves Transmission running. If the VPN health check fails, the container exits.

# Environment variables

`NATPMP_GATEWAY` is required. The rest have defaults.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `NATPMP_GATEWAY` | Yes | None | Gateway passed to `natpmpc -g` |
| `VPN_INTERFACE` | No | `wg0` | Interface used by the killswitch health check |
| `VPN_EXPECTED_IPV4` | No | `10.2.0.2` | IPv4 address that must exist on `VPN_INTERFACE` |
| `TRANSMISSION_CONFIG_DIR` | No | `/config` | Transmission config directory |
| `NATPMP_RENEW_INTERVAL_SECONDS` | No | `45` | Delay between successful NAT-PMP renewals |

# Example Kubernetes deployment

This assumes you have a WireGuard sidecar or sidecar-style init container providing the VPN interface.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Values.name }}
spec:
  replicas: 1
  revisionHistoryLimit: 3
  selector:
    matchLabels:
      app: {{ .Values.name }}
  template:
    metadata:
      labels:
        app: {{ .Values.name }}
    spec:
      initContainers:
        - image: linuxserver/wireguard:1.0.20250521
          restartPolicy: Always
          name: wg
          env:
            - name: TZ
              value: Europe/London
          volumeMounts:
            - name: wg-conf
              mountPath: /etc/wireguard/
              readOnly: true
          securityContext:
            privileged: true
            capabilities:
              add:
                - NET_ADMIN
      containers:
        - image: ghcr.io/amordue/transmission-with-natpmp:main
          imagePullPolicy: IfNotPresent
          name: transmission
          env:
            - name: NATPMP_GATEWAY
              value: 10.2.0.1
            - name: VPN_INTERFACE
              value: wg0
            - name: VPN_EXPECTED_IPV4
              value: 10.2.0.2
          volumeMounts:
            - name: data-video
              mountPath: /data
            - name: config
              mountPath: /config
      restartPolicy: Always
      volumes:
        - name: data-video
          persistentVolumeClaim:
            claimName: nfs-video-pvc
        - name: wg-conf
          secret:
            secretName: wg-conf
        - name: config
          persistentVolumeClaim:
            claimName: {{ .Values.name }}-config
```

For ProtonVPN WireGuard configs, the common values are `VPN_INTERFACE=wg0`, `VPN_EXPECTED_IPV4=10.2.0.2`, and `NATPMP_GATEWAY=10.2.0.1`.

# Publishing

The workflow in [publish.yml](.github/workflows/publish.yml) publishes to `ghcr.io/<owner>/transmission-with-natpmp`.

It runs on:

* Pushes to `main`, publishing the `main` tag
* Pushes to version tags matching `v*`, publishing semantic version tags such as `0.1.0` and `0.1`

The workflow uses the built-in `GITHUB_TOKEN` with `packages: write` permission, so no separate registry secret is required for GHCR.

# Local build

```sh
docker build -t transmission-with-natpmp:local .
```