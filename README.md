# What is this repo?

Creates a container image for use with Kubernetes, it contains transmission and natpmpc so that you can set up port forwarding through VPN providers e.g. protonvpn

# How this works

This assumes you have a sidecar container running wireguard e.g.

```
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
          value: "Europe/London"
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
      - image: alexmordue/transmission-with-natpmp:0.1.0
        imagePullPolicy: IfNotPresent
        name: transmission
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

The config from ProtonVPN will give your wg0 interface the ip 10.2.0.2 (the gateway is 10.2.0.1). The default config for transmission (included) binds to that ip.

There are three processes in this container:
* Killswitch - Causes the container to exit if wg0 disconnects (i.e. wireguard VPN is down)
* natpmpc - See https://protonvpn.com/support/port-forwarding-manual-setup (Must run in a loop to keep the tunnel open)
* Transmission

If the VPN goes down then we don't want Transmission to run so we exit. If the natpmpc port changes (i.e. `Mapped public port` reported by natpmpc changes) then transmission `settings.json` should be updated and transmission restarted.